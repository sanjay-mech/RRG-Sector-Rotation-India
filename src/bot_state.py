"""
State persistence: user subscriptions and previous RRG values (including quadrant path history).
"""
import json
import os
import logging
from datetime import datetime
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

STATE_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "bot_state.json")

DEFAULT_STATE = {
    "users": {},
    "previous_rrg": {},
    "nfo_list": [],
}

def _load() -> dict:
    if not os.path.exists(STATE_FILE):
        return dict(DEFAULT_STATE)
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Failed to load state file: {e}")
        return dict(DEFAULT_STATE)

def _save(state: dict):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        logger.error(f"Failed to save state file: {e}")

def get_user(chat_id: int) -> dict:
    state = _load()
    return state["users"].get(str(chat_id), {})

def _ensure_user(state: dict, chat_id: int):
    uid = str(chat_id)
    if uid not in state["users"]:
        state["users"][uid] = {
            "subscribed_sectors": False,
            "subscribed_nfo": False,
            "subscribed_stocks": [],
            "created_at": datetime.now().isoformat(),
        }

def subscribe_sectors(chat_id: int) -> bool:
    state = _load()
    _ensure_user(state, chat_id)
    state["users"][str(chat_id)]["subscribed_sectors"] = True
    _save(state)
    return True

def unsubscribe_sectors(chat_id: int) -> bool:
    state = _load()
    _ensure_user(state, chat_id)
    state["users"][str(chat_id)]["subscribed_sectors"] = False
    _save(state)
    return True

def subscribe_stocks(chat_id: int, symbols: List[str]) -> List[str]:
    state = _load()
    _ensure_user(state, chat_id)
    existing = set(state["users"][str(chat_id)]["subscribed_stocks"])
    added = [s for s in symbols if s not in existing]
    state["users"][str(chat_id)]["subscribed_stocks"] = list(existing | set(symbols))
    _save(state)
    return added

def unsubscribe_stocks(chat_id: int, symbols: Optional[List[str]] = None) -> List[str]:
    state = _load()
    _ensure_user(state, chat_id)
    uid = str(chat_id)
    if symbols is None:
        removed = list(state["users"][uid]["subscribed_stocks"])
        state["users"][uid]["subscribed_stocks"] = []
        _save(state)
        return removed
    current = set(state["users"][uid]["subscribed_stocks"])
    removed = [s for s in symbols if s in current]
    state["users"][uid]["subscribed_stocks"] = list(current - set(symbols))
    _save(state)
    return removed

def subscribe_nfo(chat_id: int) -> bool:
    state = _load()
    _ensure_user(state, chat_id)
    state["users"][str(chat_id)]["subscribed_nfo"] = True
    _save(state)
    return True

def unsubscribe_nfo(chat_id: int) -> bool:
    state = _load()
    _ensure_user(state, chat_id)
    state["users"][str(chat_id)]["subscribed_nfo"] = False
    _save(state)
    return True

def get_all_nfo_subscribers() -> List[int]:
    state = _load()
    return [int(uid) for uid, u in state["users"].items() if u.get("subscribed_nfo")]

def get_nfo_list() -> List[str]:
    state = _load()
    return state.get("nfo_list", [])

def set_nfo_list(symbols: List[str]):
    state = _load()
    state["nfo_list"] = symbols
    _save(state)

def unsubscribe_all(chat_id: int):
    state = _load()
    uid = str(chat_id)
    if uid in state["users"]:
        state["users"][uid]["subscribed_sectors"] = False
        state["users"][uid]["subscribed_nfo"] = False
        state["users"][uid]["subscribed_stocks"] = []
    _save(state)

def get_all_sector_subscribers() -> List[int]:
    state = _load()
    return [int(uid) for uid, u in state["users"].items() if u.get("subscribed_sectors")]

def get_all_stock_subscribers() -> Dict[int, List[str]]:
    state = _load()
    return {
        int(uid): u["subscribed_stocks"]
        for uid, u in state["users"].items()
        if u.get("subscribed_stocks")
    }

def get_previous_rrg(key: str) -> Optional[dict]:
    state = _load()
    return state["previous_rrg"].get(key)

def get_all_previous_rrg() -> Dict[str, dict]:
    state = _load()
    return dict(state["previous_rrg"])

def save_rrg_state(key: str, rs_ratio: float, momentum: float, quadrant: str, path: Optional[List[dict]] = None):
    state = _load()
    entry = {
        "rs_ratio": round(rs_ratio, 2),
        "momentum": round(momentum, 2),
        "quadrant": quadrant,
        "path": path or [{"q": quadrant, "n": 1}],
        "updated_at": datetime.now().isoformat(),
    }
    state["previous_rrg"][key] = entry
    _save(state)
