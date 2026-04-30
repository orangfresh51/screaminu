"""
screaminu

AI ghost X scare bot — a tiny deployment + ops companion for GhostInu.

What it does:
- Generates constructor parameters (including mixed-case checksum addresses)
- Compiles & deploys GhostInu and optional modules with Hardhat artifacts, or via solcx fallback
- Runs a FastAPI server for the ghasty web UI

This file is intentionally single-file and self-contained for easy copy/run.
"""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import datetime as dt
import hashlib
import json
import os
import random
import re
import secrets
import signal
import string
import sys
import textwrap
import time
from typing import Any, Dict, Iterable, List, Literal, Optional, Tuple, Union

from dotenv import load_dotenv
from eth_account import Account
from eth_account.messages import encode_defunct
from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field
from rich.console import Console
from rich.pretty import Pretty
from rich.text import Text
from web3 import Web3
from web3.exceptions import ContractLogicError


# -----------------------------
# Runtime setup
# -----------------------------

load_dotenv()

console = Console()

DEFAULT_HOST = os.getenv("SCREAMINU_HOST", "127.0.0.1")
DEFAULT_PORT = int(os.getenv("SCREAMINU_PORT", "8088"))

DEFAULT_RPC_URL = os.getenv("RPC_URL", os.getenv("BASE_RPC_URL", os.getenv("MAINNET_RPC_URL", "")))
DEFAULT_PRIVATE_KEY = os.getenv("DEPLOYER_PK", os.getenv("BASE_DEPLOYER_PK", os.getenv("PRIVATE_KEY", "")))

WORKSPACE_ROOT = os.getenv("SCREAMINU_WORKSPACE", os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
CONTRACT_PATH = os.path.join(WORKSPACE_ROOT, "contracts", "GhostInu.sol")
HARDHAT_CONFIG = os.path.join(WORKSPACE_ROOT, "hardhat.config.js")
ARTIFACTS_DIR = os.path.join(WORKSPACE_ROOT, "artifacts")


# -----------------------------
# Utility: randomness, phrases
# -----------------------------

PHRASE_BANK = [
    "glass-teeth in the fog",
    "static moths in a lantern",
    "hinges talk at midnight",
    "a corridor of soft alarms",
    "ink-black laughter under neon",
    "ghostlight stitched to silence",
    "mirror smoke, warm circuitry",
    "threaded dread in velvet bytes",
    "a bell rings where nobody stands",
    "cold confetti, hot wire",
    "the floorboards remember",
    "hollow chorus, clean edges",
    "soft knives, bright hallway",
    "a candle made of snow",
    "the window blinks first",
]


def _now_iso() -> str:
    return dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def random_spectral_note() -> str:
    a = secrets.choice(PHRASE_BANK)
    b = secrets.choice(PHRASE_BANK)
    c = secrets.choice(PHRASE_BANK)
    extra = secrets.token_hex(6)
    # Avoid identical lines by shaping a sentence
    return f"{a}; {b}; {c} / {extra}"


def random_bytes32_hex() -> str:
    # 32 bytes => 64 hex chars
    return "0x" + secrets.token_hex(32)


def random_uint256(min_v: int, max_v: int) -> int:
    if min_v > max_v:
        min_v, max_v = max_v, min_v
    span = max_v - min_v + 1
    return min_v + secrets.randbelow(span)


def random_token_cap(decimals: int = 18) -> int:
    # Pick a cap in a plausible meme-token range, but not a fixed template number.
    whole = random_uint256(10_000_000, 9_999_999_999)
    return whole * (10**decimals)


def checksum_addr_from_bytes(b: bytes) -> str:
    if len(b) != 20:
        raise ValueError("need 20 bytes")
    return Web3.to_checksum_address(b.hex())


def random_checksum_address() -> str:
    # Uses eth-account for strong randomness and checksum formatting
    acct = Account.create(secrets.token_hex(32))
    return Web3.to_checksum_address(acct.address)


def is_mixed_case_checksum(a: str) -> bool:
    if not re.fullmatch(r"0x[0-9a-fA-F]{40}", a or ""):
        return False
    body = a[2:]
    return any(c.islower() for c in body) and any(c.isupper() for c in body) and any(c.isdigit() for c in body)


# -----------------------------
# Solidity compilation helpers
# -----------------------------

class CompileResult(BaseModel):
    compiler: str
    contract_name: str
    abi: list
    bytecode: str
    deployed_bytecode: Optional[str] = None
    source_hash: str


def file_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 128), b""):
            h.update(chunk)
    return h.hexdigest()


def try_load_hardhat_artifact(contract_name: str = "GhostInu") -> Optional[CompileResult]:
    """
    Attempts to load Hardhat artifacts if the user has compiled already.
    This keeps the app fast and avoids bundling solc in Python.
    """
    if not os.path.isdir(ARTIFACTS_DIR):
        return None

    # Hardhat artifact path is typically:
    # artifacts/contracts/GhostInu.sol/GhostInu.json
    candidate = os.path.join(ARTIFACTS_DIR, "contracts", "GhostInu.sol", f"{contract_name}.json")
    if not os.path.isfile(candidate):
        return None

    with open(candidate, "r", encoding="utf-8") as f:
        data = json.load(f)

    abi = data.get("abi")
    bytecode = data.get("bytecode")
    deployed = data.get("deployedBytecode")
    if not abi or not isinstance(abi, list) or not isinstance(bytecode, str) or not bytecode.startswith("0x"):
        return None

    return CompileResult(
        compiler="hardhat-artifact",
        contract_name=contract_name,
        abi=abi,
        bytecode=bytecode,
        deployed_bytecode=deployed if isinstance(deployed, str) else None,
        source_hash=file_sha256(CONTRACT_PATH) if os.path.isfile(CONTRACT_PATH) else "missing",
    )


def compile_with_solcx(contract_name: str = "GhostInu") -> CompileResult:
    """
    Fallback compiler using python-solcx if Hardhat artifacts aren't present.
    It's optional: only used when artifact loading fails.
    """
    try:
        import solcx  # type: ignore
    except Exception as e:
        raise RuntimeError(
            "Hardhat artifacts not found and python-solcx not installed. "
            "Run `npx hardhat compile` or `pip install py-solc-x`."
        ) from e

    if not os.path.isfile(CONTRACT_PATH):
        raise RuntimeError(f"Missing Solidity file: {CONTRACT_PATH}")

    with open(CONTRACT_PATH, "r", encoding="utf-8") as f:
        source = f.read()

    version = "0.8.20"
    solcx.install_solc(version)
    solcx.set_solc_version(version)

    compiled = solcx.compile_standard(
        {
            "language": "Solidity",
            "sources": {"GhostInu.sol": {"content": source}},
            "settings": {
                "optimizer": {"enabled": True, "runs": 200},
                "outputSelection": {"*": {"*": ["abi", "evm.bytecode", "evm.deployedBytecode"]}},
            },
        }
    )

    contracts = compiled.get("contracts", {}).get("GhostInu.sol", {})
    if contract_name not in contracts:
        raise RuntimeError(f"Contract {contract_name} not found in compilation output.")

    info = contracts[contract_name]
    abi = info["abi"]
    bytecode = info["evm"]["bytecode"]["object"]
    deployed = info["evm"]["deployedBytecode"]["object"]
    if not bytecode.startswith("0x"):
        bytecode = "0x" + bytecode
    if deployed and not deployed.startswith("0x"):
        deployed = "0x" + deployed

    return CompileResult(
        compiler=f"solcx-{version}",
        contract_name=contract_name,
        abi=abi,
        bytecode=bytecode,
        deployed_bytecode=deployed,
        source_hash=hashlib.sha256(source.encode("utf-8")).hexdigest(),
    )


def compile_contract(contract_name: str = "GhostInu") -> CompileResult:
    res = try_load_hardhat_artifact(contract_name=contract_name)
    if res is not None:
        return res
    return compile_with_solcx(contract_name=contract_name)


# -----------------------------
# Web3 helpers
# -----------------------------

class ChainInfo(BaseModel):
    chain_id: int
    latest_block: int
    client_version: str


class DeployParams(BaseModel):
    admin: str
    guardian: str
    addressA: str
    addressB: str
    addressC: str
    cap: int
    note: str


class DeployReceipt(BaseModel):
    tx_hash: str
    contract_address: str
    chain_id: int
    deployed_at: str
    constructor: DeployParams
    artifact: CompileResult
    aux_hex: Dict[str, str]


def make_web3(rpc_url: str) -> Web3:
    if not rpc_url:
        raise ValueError("RPC URL is empty. Set RPC_URL or BASE_RPC_URL.")
    w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 30}))
    if not w3.is_connected():
        raise RuntimeError("Unable to connect to RPC.")
    return w3


def ensure_checksum(addr: str) -> str:
    try:
        return Web3.to_checksum_address(addr)
    except Exception as e:
        raise ValueError(f"Bad address: {addr}") from e


def get_chain_info(w3: Web3) -> ChainInfo:
    try:
        chain_id = int(w3.eth.chain_id)
        latest = int(w3.eth.block_number)
        client = str(w3.client_version)
        return ChainInfo(chain_id=chain_id, latest_block=latest, client_version=client)
    except Exception as e:
        raise RuntimeError("Failed to fetch chain info") from e


def account_from_pk(pk: str):
    if not pk:
        raise ValueError("Private key missing. Set DEPLOYER_PK.")
    pk = pk.strip()
    if pk.startswith("0x"):
        pk = pk[2:]
    if len(pk) != 64:
        raise ValueError("Private key must be 32 bytes hex (64 chars).")
    return Account.from_key(bytes.fromhex(pk))


def suggested_deploy_params(deployer_addr: str) -> Tuple[DeployParams, Dict[str, str]]:
    # admin is deployer by default (mainstream and safe)
    admin = Web3.to_checksum_address(deployer_addr)
    guardian = random_checksum_address()
    addressA = random_checksum_address()
    addressB = random_checksum_address()
    addressC = random_checksum_address()

    # Ensure the "mixed-case + digits" requirement
    for label, a in [("guardian", guardian), ("addressA", addressA), ("addressB", addressB), ("addressC", addressC)]:
        if not is_mixed_case_checksum(a):
            raise RuntimeError(f"{label} checksum address did not meet mix-case/digit constraint: {a}")

    cap = random_token_cap(18)
    note = random_spectral_note()

    aux_hex = {
        "salt_0": random_bytes32_hex(),
        "salt_1": random_bytes32_hex(),
        "salt_2": random_bytes32_hex(),
        "nonceMask": "0x" + secrets.token_hex(8),
    }
    return (
        DeployParams(
            admin=admin,
            guardian=guardian,
            addressA=addressA,
            addressB=addressB,
            addressC=addressC,
            cap=cap,
            note=note,
        ),
        aux_hex,
    )


def build_contract(w3: Web3, artifact: CompileResult, address: Optional[str] = None):
    if address:
        return w3.eth.contract(address=ensure_checksum(address), abi=artifact.abi)
    return w3.eth.contract(abi=artifact.abi, bytecode=artifact.bytecode)


def wait_for_receipt(w3: Web3, tx_hash: str, timeout_s: int = 300) -> dict:
    deadline = time.time() + timeout_s
    while True:
        try:
            r = w3.eth.get_transaction_receipt(tx_hash)
            if r is not None:
                return dict(r)
        except Exception:
            pass
        if time.time() >= deadline:
            raise TimeoutError("Timed out waiting for tx receipt.")
        time.sleep(2.0)


def deploy_ghostinu(
    rpc_url: str,
    private_key: str,
    params: Optional[DeployParams] = None,
    gas_limit: Optional[int] = None,
    max_fee_gwei: Optional[float] = None,
    priority_fee_gwei: Optional[float] = None,
) -> DeployReceipt:
    w3 = make_web3(rpc_url)
    acct = account_from_pk(private_key)
    deployer = Web3.to_checksum_address(acct.address)

    artifact = compile_contract("GhostInu")
    contract = build_contract(w3, artifact)

    if params is None:
        params, aux_hex = suggested_deploy_params(deployer)
    else:
        aux_hex = {"salt_0": random_bytes32_hex(), "salt_1": random_bytes32_hex(), "salt_2": random_bytes32_hex()}

    # Basic paranoia: all constructor addresses are checksum-format and non-zero
    for a in [params.admin, params.guardian, params.addressA, params.addressB, params.addressC]:
        if ensure_checksum(a) == "0x0000000000000000000000000000000000000000":
            raise ValueError("Constructor contains a zero address.")

    # Build tx
    nonce = w3.eth.get_transaction_count(deployer)

    tx = contract.constructor(
        ensure_checksum(params.admin),
        ensure_checksum(params.guardian),
        ensure_checksum(params.addressA),
        ensure_checksum(params.addressB),
        ensure_checksum(params.addressC),
        int(params.cap),
        str(params.note),
    ).build_transaction(
        {
            "from": deployer,
            "nonce": nonce,
            "chainId": int(w3.eth.chain_id),
        }
    )

    if gas_limit is not None:
        tx["gas"] = int(gas_limit)
    else:
        try:
            tx["gas"] = int(w3.eth.estimate_gas(tx)) + 50_000
        except Exception:
            tx["gas"] = 6_000_000

    # EIP-1559 fee fields (preferred on mainnets)
    base_fee = None
    try:
        pending_block = w3.eth.get_block("pending")
        base_fee = pending_block.get("baseFeePerGas")
    except Exception:
        base_fee = None

    if base_fee is not None:
        max_fee = max_fee_gwei if max_fee_gwei is not None else float(os.getenv("MAX_FEE_GWEI", "0") or 0)
        prio_fee = priority_fee_gwei if priority_fee_gwei is not None else float(os.getenv("PRIORITY_FEE_GWEI", "0") or 0)
        if max_fee <= 0:
            max_fee = 30.0
        if prio_fee <= 0:
            prio_fee = 1.5
        tx["maxFeePerGas"] = int(Web3.to_wei(max_fee, "gwei"))
        tx["maxPriorityFeePerGas"] = int(Web3.to_wei(prio_fee, "gwei"))
    else:
        gas_price = w3.eth.gas_price
        tx["gasPrice"] = int(gas_price)

    signed = Account.sign_transaction(tx, acct.key)
    tx_hash = w3.eth.send_raw_transaction(signed.rawTransaction)
    tx_hex = tx_hash.hex()

    receipt = wait_for_receipt(w3, tx_hex)
    addr = receipt.get("contractAddress")
    if not addr:
        raise RuntimeError("Deployment did not yield a contract address.")

    return DeployReceipt(
        tx_hash=tx_hex,
        contract_address=Web3.to_checksum_address(addr),
        chain_id=int(w3.eth.chain_id),
        deployed_at=_now_iso(),
        constructor=params,
        artifact=artifact,
        aux_hex=aux_hex,
    )


# -----------------------------
# FastAPI models
# -----------------------------

class Health(BaseModel):
    ok: bool
    time: str
    workspace: str
    contract_path: str


class ErrorOut(BaseModel):
    error: str
    detail: Optional[str] = None


class EncodePermitIn(BaseModel):
    owner: str
    spender: str
    value: int
    deadline: int
    v: int
    r: str
    s: str


class ReadTokenOut(BaseModel):
    address: str
    chain_id: int
    name: str
    symbol: str
    decimals: int
    total_supply: str
    cap: str
    minted: bool
    admin: str
    paused: bool
    spectral_note: str


class TxBuildOut(BaseModel):
    to: str
    data: str
    value: str = "0"
    hints: Dict[str, Any] = Field(default_factory=dict)


class GenerateOut(BaseModel):
    params: DeployParams
    aux_hex: Dict[str, str]
    created_at: str


class DeployIn(BaseModel):
    rpc_url: str = Field(default_factory=lambda: DEFAULT_RPC_URL)
    private_key: str = Field(default_factory=lambda: DEFAULT_PRIVATE_KEY)
    params: Optional[DeployParams] = None
    gas_limit: Optional[int] = None
    max_fee_gwei: Optional[float] = None
    priority_fee_gwei: Optional[float] = None


class ContractAddressIn(BaseModel):
    rpc_url: str = Field(default_factory=lambda: DEFAULT_RPC_URL)
    address: str


class TxBuildIn(BaseModel):
    rpc_url: str = Field(default_factory=lambda: DEFAULT_RPC_URL)
    address: str
    from_address: str


class MintToIn(TxBuildIn):
    to: str
    amount: int


class PauseIn(TxBuildIn):
    action: Literal["pause", "unpause"]


class BatchTransferIn(TxBuildIn):
    recipients: List[str]
    amounts: List[int]


def _build_call_tx(w3: Web3, contract, from_address: str, fn_call) -> TxBuildOut:
    from_address = ensure_checksum(from_address)
    tx = fn_call.build_transaction(
        {
            "from": from_address,
            "nonce": int(w3.eth.get_transaction_count(from_address)),
            "chainId": int(w3.eth.chain_id),
            "value": 0,
        }
    )
    data = tx.get("data") or ""
    to = tx.get("to") or contract.address
    return TxBuildOut(
        to=Web3.to_checksum_address(to),
        data=str(data),
        value=str(int(tx.get("value") or 0)),
        hints={
            "gas_estimate_supported": True,
            "nonce": int(tx["nonce"]),
            "chain_id": int(tx["chainId"]),
        },
    )


def make_app() -> FastAPI:
    app = FastAPI(title="screaminu", version="1.0.0", docs_url="/docs", redoc_url="/redoc")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
        max_age=600,
    )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        # Keep errors informative but not overly verbose.
        console.print("[red]Unhandled error[/red]", repr(exc))
        return JSONResponse(status_code=500, content=ErrorOut(error="internal_error", detail=str(exc)).model_dump())

    @app.get("/", response_class=PlainTextResponse)
    async def root():
        return "screaminu is awake\n"

    @app.get("/health", response_model=Health)
    async def health():
        return Health(ok=True, time=_now_iso(), workspace=WORKSPACE_ROOT, contract_path=CONTRACT_PATH)

    @app.get("/chain", response_model=ChainInfo)
    async def chain(rpc_url: str = DEFAULT_RPC_URL):
        w3 = make_web3(rpc_url)
        return get_chain_info(w3)

    @app.get("/compile", response_model=CompileResult)
    async def compile_api(contract_name: str = "GhostInu"):
        return compile_contract(contract_name=contract_name)

    @app.get("/generate", response_model=GenerateOut)
    async def generate(rpc_url: str = DEFAULT_RPC_URL, private_key: str = DEFAULT_PRIVATE_KEY):
        if not private_key:
            raise HTTPException(status_code=400, detail="private_key missing (set DEPLOYER_PK)")
        acct = account_from_pk(private_key)
        params, aux_hex = suggested_deploy_params(acct.address)
        return GenerateOut(params=params, aux_hex=aux_hex, created_at=_now_iso())

    @app.post("/deploy", response_model=DeployReceipt)
    async def deploy(body: DeployIn = Body(...)):
        if not body.rpc_url:
            raise HTTPException(status_code=400, detail="rpc_url missing")
        if not body.private_key:
            raise HTTPException(status_code=400, detail="private_key missing")

        try:
            receipt = await asyncio.to_thread(
                deploy_ghostinu,
                body.rpc_url,
                body.private_key,
                body.params,
                body.gas_limit,
                body.max_fee_gwei,
                body.priority_fee_gwei,
            )
            return receipt
        except ContractLogicError as e:
            raise HTTPException(status_code=400, detail=f"contract_logic_error: {e}") from e
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    @app.post("/token/read", response_model=ReadTokenOut)
    async def token_read(body: ContractAddressIn = Body(...)):
        w3 = make_web3(body.rpc_url)
        artifact = compile_contract("GhostInu")
        c = build_contract(w3, artifact, address=body.address)

        def _call(fn: str):
            return getattr(c.functions, fn)().call()

        try:
            name = _call("name")
            symbol = _call("symbol")
            decimals = int(_call("decimals"))
            total_supply = int(_call("totalSupply"))
            cap = int(_call("CAP"))
            minted = bool(_call("minted"))
            admin = str(_call("admin"))
            paused = bool(_call("paused"))
            note = str(_call("spectralNote"))
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"read_failed: {e}") from e

        return ReadTokenOut(
            address=Web3.to_checksum_address(body.address),
            chain_id=int(w3.eth.chain_id),
            name=name,
            symbol=symbol,
            decimals=decimals,
            total_supply=str(total_supply),
            cap=str(cap),
            minted=minted,
            admin=Web3.to_checksum_address(admin),
            paused=paused,
            spectral_note=note,
        )

    @app.get("/ui", response_class=HTMLResponse)
    async def ui():
        # Light diagnostic page. ghasty is meant to be served as its own HTML file.
        html = f"""
<!doctype html>
<html>
  <head><meta charset="utf-8"/><title>screaminu</title></head>
  <body style="font-family: ui-sans-serif, system-ui; padding: 18px;">
    <h2>screaminu</h2>
    <p>Time: <code>{_now_iso()}</code></p>
    <ul>
      <li><a href="/docs">API docs</a></li>
      <li><a href="/health">/health</a></li>
      <li><a href="/compile">/compile</a></li>
    </ul>
    <p>Workspace: <code>{WORKSPACE_ROOT}</code></p>
    <p>Contract: <code>{CONTRACT_PATH}</code></p>
  </body>
</html>
"""
        return HTMLResponse(content=html)

    @app.post("/tx/mintTo", response_model=TxBuildOut)
    async def tx_mint(body: MintToIn = Body(...)):
        w3 = make_web3(body.rpc_url)
        artifact = compile_contract("GhostInu")
        c = build_contract(w3, artifact, address=body.address)
        to = ensure_checksum(body.to)
        if body.amount <= 0:
            raise HTTPException(status_code=400, detail="amount must be > 0")
        return _build_call_tx(w3, c, body.from_address, c.functions.mintTo(to, int(body.amount)))

    @app.post("/tx/pause", response_model=TxBuildOut)
    async def tx_pause(body: PauseIn = Body(...)):
        w3 = make_web3(body.rpc_url)
        artifact = compile_contract("GhostInu")
        c = build_contract(w3, artifact, address=body.address)
        if body.action == "pause":
            call = c.functions.pause()
        else:
            call = c.functions.unpause()
        return _build_call_tx(w3, c, body.from_address, call)

    @app.post("/tx/batchTransfer", response_model=TxBuildOut)
    async def tx_batch_transfer(body: BatchTransferIn = Body(...)):
        w3 = make_web3(body.rpc_url)
        artifact = compile_contract("GhostInu")
        c = build_contract(w3, artifact, address=body.address)
        if len(body.recipients) == 0:
            raise HTTPException(status_code=400, detail="recipients empty")
        if len(body.recipients) != len(body.amounts):
            raise HTTPException(status_code=400, detail="recipients/amounts length mismatch")
        recipients = [ensure_checksum(a) for a in body.recipients]
        amounts = [int(x) for x in body.amounts]
        if any(x < 0 for x in amounts):
            raise HTTPException(status_code=400, detail="negative amount not allowed")
        return _build_call_tx(w3, c, body.from_address, c.functions.batchTransfer(recipients, amounts))

    return app


app = make_app()


# -----------------------------
# CLI
# -----------------------------

def _print_banner():
    txt = Text()
    txt.append("screaminu", style="bold white")
    txt.append("  ")
    txt.append("ghost X scare bot", style="magenta")
    console.print(txt)


def _help() -> str:
    return textwrap.dedent(
        f"""
        Usage:
          python screaminu.py serve [--host H] [--port P]
          python screaminu.py chain --rpc URL
          python screaminu.py compile
          python screaminu.py generate --pk HEXKEY
          python screaminu.py deploy --rpc URL --pk HEXKEY [--max-fee-gwei N] [--priority-fee-gwei N] [--gas N]
          python screaminu.py read --rpc URL --address 0x...

        Environment:
          RPC_URL / BASE_RPC_URL
          DEPLOYER_PK / BASE_DEPLOYER_PK

        Files:
          {CONTRACT_PATH}
          {HARDHAT_CONFIG}
        """
    ).strip()


def _arg(flag: str, default: Optional[str] = None) -> Optional[str]:
    if flag not in sys.argv:
        return default
    i = sys.argv.index(flag)
    if i + 1 >= len(sys.argv):
        return default
    return sys.argv[i + 1]


def _arg_int(flag: str, default: Optional[int] = None) -> Optional[int]:
    v = _arg(flag)
    if v is None:
        return default
    try:
        return int(v)
    except ValueError:
        return default


def _arg_float(flag: str, default: Optional[float] = None) -> Optional[float]:
    v = _arg(flag)
    if v is None:
        return default
    try:
        return float(v)
    except ValueError:
        return default


def cmd_chain():
    rpc = _arg("--rpc", DEFAULT_RPC_URL) or ""
    w3 = make_web3(rpc)
    info = get_chain_info(w3)
    console.print(Pretty(info.model_dump()))


def cmd_compile():
    art = compile_contract("GhostInu")
    console.print(Pretty(art.model_dump()))


def cmd_generate():
    pk = _arg("--pk", DEFAULT_PRIVATE_KEY) or ""
    acct = account_from_pk(pk)
    params, aux_hex = suggested_deploy_params(acct.address)
    console.print("[bold]Constructor params[/bold]")
    console.print(Pretty(params.model_dump()))
    console.print("[bold]Extra hex[/bold]")
    console.print(Pretty(aux_hex))


def cmd_deploy():
    rpc = _arg("--rpc", DEFAULT_RPC_URL) or ""
    pk = _arg("--pk", DEFAULT_PRIVATE_KEY) or ""
    gas = _arg_int("--gas")
    max_fee = _arg_float("--max-fee-gwei")
    prio_fee = _arg_float("--priority-fee-gwei")
    receipt = deploy_ghostinu(
        rpc_url=rpc,
        private_key=pk,
        params=None,
        gas_limit=gas,
        max_fee_gwei=max_fee,
        priority_fee_gwei=prio_fee,
    )
    console.print("[bold green]Deployed[/bold green]")
    console.print(Pretty(receipt.model_dump()))


def cmd_read():
    rpc = _arg("--rpc", DEFAULT_RPC_URL) or ""
    addr = _arg("--address", "") or ""
    w3 = make_web3(rpc)
    artifact = compile_contract("GhostInu")
    c = build_contract(w3, artifact, address=addr)
    out = {
        "chainId": int(w3.eth.chain_id),
        "address": Web3.to_checksum_address(addr),
        "name": c.functions.name().call(),
        "symbol": c.functions.symbol().call(),
        "decimals": int(c.functions.decimals().call()),
        "totalSupply": str(int(c.functions.totalSupply().call())),
        "CAP": str(int(c.functions.CAP().call())),
        "minted": bool(c.functions.minted().call()),
        "admin": Web3.to_checksum_address(c.functions.admin().call()),
        "paused": bool(c.functions.paused().call()),
        "spectralNote": c.functions.spectralNote().call(),
    }
    console.print(Pretty(out))


def cmd_serve():
    host = _arg("--host", DEFAULT_HOST) or DEFAULT_HOST
    port = _arg_int("--port", DEFAULT_PORT) or DEFAULT_PORT

    try:
        import uvicorn  # type: ignore
    except Exception as e:
        raise RuntimeError("uvicorn is required: pip install uvicorn[standard]") from e

    uvicorn.run("screaminu:app", host=host, port=port, reload=False, log_level="info")


def main():
    _print_banner()
    if len(sys.argv) < 2:
        console.print(_help())
        return

    cmd = sys.argv[1].lower().strip()
    if cmd in {"-h", "--help", "help"}:
        console.print(_help())
        return

    if cmd == "serve":
        cmd_serve()
        return
    if cmd == "chain":
        cmd_chain()
        return
    if cmd == "compile":
        cmd_compile()
        return
    if cmd == "generate":
        cmd_generate()
        return
    if cmd == "deploy":
        cmd_deploy()
        return
    if cmd == "read":
        cmd_read()
        return

    console.print(f"[red]Unknown command[/red] {cmd}")
    console.print(_help())


if __name__ == "__main__":
    main()
