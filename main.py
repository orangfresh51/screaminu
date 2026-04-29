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
