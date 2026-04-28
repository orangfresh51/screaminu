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
