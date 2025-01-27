from configured_logger import new_logger
from datetime import timedelta
from locust import User, events, runners
from retrying import retry
import abc
import utils
import mocknet_helpers
import key
import transaction
import cluster
import base64
import json
import base58
import ctypes
import logging
import multiprocessing
import pathlib
import requests
import sys
import time

sys.path.append(str(pathlib.Path(__file__).resolve().parents[4] / 'lib'))

DEFAULT_TRANSACTION_TTL = timedelta(minutes=30)
logger = new_logger(level=logging.WARN)


def is_key_error(exception):
    return isinstance(exception, KeyError)


def is_tx_unknown_error(exception):
    return isinstance(exception, TxUnknownError)


class Account:

    def __init__(self, key):
        self.key = key
        self.current_nonce = multiprocessing.Value(ctypes.c_ulong, 0)

    # Race condition: maybe the account was created but the RPC interface
    # doesn't display it yet, which returns an empty result and raises a
    # `KeyError`.
    # (not quite sure how this happens but it did happen to me on localnet)
    @retry(wait_exponential_multiplier=500,
           wait_exponential_max=10000,
           stop_max_attempt_number=5,
           retry_on_exception=is_key_error)
    def refresh_nonce(self, node):
        with self.current_nonce.get_lock():
            self.current_nonce.value = mocknet_helpers.get_nonce_for_key(
                self.key,
                addr=node.rpc_addr()[0],
                port=node.rpc_addr()[1],
                logger=logger,
            )

    def use_nonce(self):
        with self.current_nonce.get_lock():
            new_nonce = self.current_nonce.value + 1
            self.current_nonce.value = new_nonce
            return new_nonce


class Transaction:
    """
    A transaction future.
    """

    ID = 0

    def __init__(self):
        self.id = Transaction.ID
        Transaction.ID += 1

        # The transaction id hash
        #
        # str if the transaction has been submitted and may eventually conclude.
        # FIXME: this is currently not set in some cases
        self.transaction_id = None

    @abc.abstractmethod
    def sign_and_serialize(self, block_hash) -> bytes:
        """
        Each transaction class is supposed to define this method to serialize and
        sign the transaction and return the raw message to be sent.
        """

    @abc.abstractmethod
    def sender_id(self) -> str:
        """
        Account id of the sender that signs the tx, which must be known to map
        the tx-result request to the right shard.
        """


class Deploy(Transaction):

    def __init__(self, account, contract, name):
        super().__init__()
        self.account = account
        self.contract = contract
        self.name = name

    def sign_and_serialize(self, block_hash) -> bytes:
        account = self.account
        logger.info(f"deploying {self.name} to {account.key.account_id}")
        wasm_binary = utils.load_binary_file(self.contract)
        return transaction.sign_deploy_contract_tx(account.key, wasm_binary,
                                                   account.use_nonce(),
                                                   block_hash)

    def sender_id(self) -> str:
        return self.account.key.account_id


class CreateSubAccount(Transaction):

    def __init__(self, sender, sub_key, balance=50.0):
        super().__init__()
        self.sender = sender
        self.sub_key = sub_key
        self.balance = balance

    def sign_and_serialize(self, block_hash) -> bytes:
        sender = self.sender
        sub = self.sub_key
        logger.debug(f"creating {sub.account_id}")
        return transaction.sign_create_account_with_full_access_key_and_balance_tx(
            sender.key, sub.account_id, sub, int(self.balance * 1E24),
            sender.use_nonce(), block_hash)

    def sender_id(self) -> str:
        return self.sender.key.account_id


class NearNodeProxy:
    """
    Wrapper around a RPC node connection that tracks requests on locust.
    """

    def __init__(self, host, request_event):
        self.request_event = request_event
        url, port = host.split(":")
        self.node = cluster.RpcNode(url, port)
        self.session = requests.Session()

    def send_tx(self, tx: Transaction, locust_name):
        """
        Send a transaction and return the result, no retry attempted.
        """
        block_hash = base58.b58decode(self.node.get_latest_block().hash)
        signed_tx = tx.sign_and_serialize(block_hash)

        meta = {
            "request_type": "near-rpc",
            "name": locust_name,
            "start_time": time.time(),
            "response_time": 0,  # overwritten later  with end-to-end time
            "response_length": 0,  # overwritten later
            "response": None,  # overwritten later
            "context": {},  # not used  right now
            "exception": None,  # maybe overwritten later
        }
        start_perf_counter = time.perf_counter()

        # async submit
        submit_raw_response = self.post_json(
            "broadcast_tx_async", [base64.b64encode(signed_tx).decode('utf8')])
        meta["response_length"] = len(submit_raw_response.text)

        # extract transaction ID from response, it should be "{ "result": "id...." }"
        submit_response = submit_raw_response.json()
        if not "result" in submit_response:
            meta["exception"] = RpcError(submit_response,
                                         message="Didn't get a TX ID")
            meta["response"] = submit_response.content
        else:
            tx.transaction_id = submit_response["result"]
            try:
                # using retrying lib here to poll until a response is ready
                self.poll_tx_result(meta, [tx.transaction_id, tx.sender_id()])
            except NearError as err:
                logging.warn(f"marking an error {err.message}, {err.details}")
                meta["exception"] = err

        meta["response_time"] = (time.perf_counter() -
                                 start_perf_counter) * 1000

        # Track request + response in Locust
        self.request_event.fire(**meta)
        return meta["response"]

    def post_json(self, method: str, params: list(str)):
        j = {
            "method": method,
            "params": params,
            "id": "dontcare",
            "jsonrpc": "2.0"
        }
        return self.session.post(url="http://%s:%s" % self.node.rpc_addr(),
                                 json=j)

    @retry(wait_fixed=500,
           stop_max_delay=DEFAULT_TRANSACTION_TTL / timedelta(milliseconds=1),
           retry_on_exception=is_tx_unknown_error)
    def poll_tx_result(self, meta, params):
        # poll for tx result, using "EXPERIMENTAL_tx_status" which waits for
        # all receipts to finish rather than just the first one, as "tx" would do
        result_response = self.post_json("EXPERIMENTAL_tx_status", params)

        try:
            meta["response"] = evaluate_rpc_result(result_response.json())
        except:
            # Store raw response to improve error-reporting.
            meta["response"] = result_response.content
            raise


class NearUser(User):
    abstract = True
    id_counter = 0
    INIT_BALANCE = 100.0
    funding_account: Account

    @classmethod
    def get_next_id(cls):
        cls.id_counter += 1
        return cls.id_counter

    @classmethod
    def generate_account_id(cls, id) -> str:
        # Pseudo-random 6-digit prefix to spread the users in the state tree
        # TODO: Also make sure these are spread evenly across shards
        prefix = str(hash(str(id)))[-6:]
        return f"{prefix}_user{id}.{cls.funding_account.key.account_id}"

    def __init__(self, environment):
        super().__init__(environment)
        assert self.host is not None, "Near user requires the RPC node address"
        self.node = NearNodeProxy(self.host, environment.events.request)
        self.id = NearUser.get_next_id()
        self.account_id = NearUser.generate_account_id(self.id)

    def on_start(self):
        """
        Called once per user, creating the account on chain
        """
        self.account = Account(key.Key.from_random(self.account_id))
        self.send_tx_retry(
            CreateSubAccount(NearUser.funding_account,
                             self.account.key,
                             balance=NearUser.INIT_BALANCE))

        self.account.refresh_nonce(self.node.node)

    def send_tx(self, tx: Transaction, locust_name="generic send_tx"):
        """
        Send a transaction and return the result, no retry attempted.
        """
        return self.node.send_tx(tx, locust_name)

    def send_tx_retry(self,
                      tx: Transaction,
                      locust_name="generic send_tx_retry"):
        """
        Send a transaction and retry until it succeeds
        """
        # expected error: UNKNOWN_TRANSACTION means TX has not been executed yet
        # other errors: probably bugs in the test setup (e.g. invalid signer)
        # this method is very simple and just retries no matter the kind of
        # error, as long as it is one defined by us (inherits from NearError)
        while True:
            try:
                result = self.node.send_tx(tx, locust_name=locust_name)
                return result
            except NearError as error:
                logger.warn(
                    f"transaction {tx.transaction_id} failed: {error}, retrying in 0.25s"
                )
                time.sleep(0.25)


def send_transaction(node, tx):
    """
    Send a transaction without a user.
    Retry until it is successful.
    Used for setting up accounts before actual users start their load.
    """
    while True:
        block_hash = base58.b58decode(node.get_latest_block().hash)
        signed_tx = tx.sign_and_serialize(block_hash)
        tx_result = node.send_tx_and_wait(signed_tx, timeout=20)
        success = "error" not in tx_result
        if success:
            logger.debug(
                f"transaction {tx.transaction_id} (for no account) is successful: {tx_result}"
            )
            return True, tx_result
        elif "UNKNOWN_TRANSACTION" in tx_result:
            logger.debug(
                f"transaction {tx.transaction_id} (for no account) timed out")
        else:
            logger.warn(
                f"transaction {tx.transaction_id} (for no account) is not successful: {tx_result}"
            )
        logger.info(f"re-submitting transaction {tx.transaction_id}")


class NearError(Exception):

    def __init__(self, message, details):
        self.message = message
        self.details = details
        super().__init__(message)


class RpcError(NearError):

    def __init__(self, error, message="RPC returned an error"):
        super().__init__(message, error)


class TxUnknownError(RpcError):

    def __init__(
        self,
        message="RPC does not know the result of this TX, probably it is not executed yet"
    ):
        super().__init__(message)


class TxError(NearError):

    def __init__(self,
                 status,
                 message="Transaction to receipt conversion failed"):
        super().__init__(message, status)


class ReceiptError(NearError):

    def __init__(self, status, receipt_id, message="Receipt execution failed"):
        super().__init__(message, f"id={receipt_id} {status}")


def evaluate_rpc_result(rpc_result):
    """
    Take the json RPC response and translate it into success
    and failure cases. Failures are raised as exceptions.
    """
    if "error" in rpc_result:
        if rpc_result["error"]["cause"]["name"] == "UNKNOWN_TRANSACTION":
            raise TxUnknownError("UNKNOWN_TRANSACTION")
        else:
            raise RpcError(rpc_result["error"])

    result = rpc_result["result"]
    transaction_outcome = result["transaction_outcome"]
    if not "SuccessReceiptId" in transaction_outcome["outcome"]["status"]:
        raise TxError(transaction_outcome["outcome"]["status"])

    receipt_outcomes = result["receipts_outcome"]
    for receipt in receipt_outcomes:
        # For each receipt, we get
        # `{ "outcome": { ..., "status": { <ExecutionStatusView>: "..." } } }`
        # and the key for `ExecutionStatusView` tells us whether it was successful
        status = list(receipt["outcome"]["status"].keys())[0]

        if status == "Unknown":
            raise ReceiptError(receipt["outcome"],
                               receipt["id"],
                               message="Unknown receipt result")
        if status == "Failure":
            raise ReceiptError(receipt["outcome"], receipt["id"])
        if not status in ["SuccessReceiptId", "SuccessValue"]:
            raise ReceiptError(receipt["outcome"],
                               receipt["id"],
                               message="Unexpected status")

    return result


# called once per process before user initialization
@events.init.add_listener
def on_locust_init(environment, **kwargs):
    # Note: These setup requests are not tracked by locust because we use our own http session
    host, port = environment.host.split(":")
    node = cluster.RpcNode(host, port)

    master_funding_key = key.Key.from_json_file(
        environment.parsed_options.funding_key)
    master_funding_account = Account(master_funding_key)

    funding_account = None
    # every worker needs a funding account to create its users, eagerly create them in the master
    if isinstance(environment.runner, runners.MasterRunner):
        num_funding_accounts = environment.parsed_options.max_workers
        funding_balance = 10000 * NearUser.INIT_BALANCE
        # TODO: Create accounts in parallel
        for id in range(num_funding_accounts):
            account_id = f"funds_worker_{id}.{master_funding_account.key.account_id}"
            worker_key = key.Key.from_seed_testonly(account_id, account_id)
            logger.info(f"Creating {account_id}")
            send_transaction(
                node,
                CreateSubAccount(master_funding_account,
                                 worker_key,
                                 balance=funding_balance))
        funding_account = master_funding_account
    elif isinstance(environment.runner, runners.WorkerRunner):
        worker_id = environment.runner.worker_index
        worker_account_id = f"funds_worker_{worker_id}.{master_funding_account.key.account_id}"
        worker_key = key.Key.from_seed_testonly(worker_account_id,
                                                worker_account_id)
        funding_account = Account(worker_key)
    elif isinstance(environment.runner, runners.LocalRunner):
        funding_account = master_funding_account
    else:
        raise SystemExit(
            f"unexpected runner class {environment.runner.__class__.__name__}")

    NearUser.funding_account = funding_account
    environment.master_funding_account = master_funding_account


# Add custom CLI args here, will be available in `environment.parsed_options`
@events.init_command_line_parser.add_listener
def _(parser):
    parser.add_argument(
        "--funding-key",
        required=True,
        help="account to use as source of NEAR for account creation")
    parser.add_argument(
        "--max-workers",
        type=int,
        required=False,
        default=16,
        help="How many funding accounts to generate for workers")
