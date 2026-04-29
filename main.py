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
