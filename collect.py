#!/usr/bin/env python3
"""
$SI site data collector. Runs on GitHub Actions every ~10 minutes.

Reads config.json, pulls on-chain activity, and writes:
  data.json   the public numbers the website shows
  state.json  bookkeeping so each run only fetches what is new

Tracks two things, both straight from the blockchain:
  1. the agent wallet: SOL balance and every buy / sell / deposit
  2. $LMAO paid to $SI holders by Pump.fun's holder rewards vault
     (switches on once "reward_vault" is filled in config.json)

Standard library only. Uses Helius RPC when the HELIUS_API_KEY secret is set,
otherwise the public Solana RPC (slower, rate limited).
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "config.json"
DATA = ROOT / "data.json"
STATE = ROOT / "state.json"

WSOL = "So11111111111111111111111111111111111111112"
LAMPORTS = 1_000_000_000
MAX_EVENTS_KEPT = 200        # agent events kept in state
MAX_EVENTS_PUBLIC = 40       # agent events shown on the site
FIRST_RUN_AGENT = 300        # how far back to look on the very first run
FIRST_RUN_VAULT = 1000
MAX_NEW_PER_RUN = 400        # cap per run so a run never times out; the rest come next run
DROP_GAP_SECONDS = 15 * 60   # vault payouts closer together than this count as one drop
TIME_BUDGET = 240            # seconds; stop fetching and save what we have
START = time.monotonic()
NOW = int(time.time())


def env(name):
    return (os.environ.get(name) or "").strip()


def rpc_url():
    key = env("HELIUS_API_KEY")
    if env("RPC_URL"):
        return env("RPC_URL")
    return f"https://mainnet.helius-rpc.com/?api-key={key}" if key else "https://api.mainnet-beta.solana.com"


def http_json(url, payload=None, tries=4):
    data = json.dumps(payload).encode() if payload is not None else None
    for attempt in range(tries):
        req = urllib.request.Request(url, data=data, headers={"content-type": "application/json", "user-agent": "si-site/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < tries - 1:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise
        except (urllib.error.URLError, TimeoutError):
            if attempt < tries - 1:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise


_id = [0]


def rpc(method, params):
    _id[0] += 1
    res = http_json(rpc_url(), {"jsonrpc": "2.0", "id": _id[0], "method": method, "params": params})
    if "error" in res:
        raise RuntimeError(f"{method}: {res['error']}")
    return res.get("result")


def out_of_time():
    return time.monotonic() - START > TIME_BUDGET


def new_signatures(address, last_sig, first_run_limit):
    """Signatures newer than last_sig, oldest first (at most MAX_NEW_PER_RUN; the rest come next run).
    On the very first run: the latest first_run_limit."""
    sigs, before, pages = [], None, 0
    while not out_of_time() and pages < 20:
        limit = 1000 if last_sig else min(1000, first_run_limit - len(sigs))
        if limit <= 0:
            break
        opts = {"limit": limit, "commitment": "confirmed"}
        if last_sig:
            opts["until"] = last_sig
        if before:
            opts["before"] = before
        page = rpc("getSignaturesForAddress", [address, opts]) or []
        pages += 1
        if not page:
            break
        sigs.extend(page)
        before = page[-1]["signature"]
        if len(page) < limit:
            break
    sigs.reverse()  # oldest first, so state advances in order and never skips
    return sigs[:MAX_NEW_PER_RUN] if last_sig else sigs


def get_tx(sig):
    return rpc("getTransaction", [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0, "commitment": "confirmed"}])


def keys_of(tx):
    out = []
    for k in tx["transaction"]["message"]["accountKeys"]:
        out.append(k["pubkey"] if isinstance(k, dict) else k)
    # v0 transactions: loaded addresses come after the static keys
    loaded = (tx.get("meta") or {}).get("loadedAddresses") or {}
    out += loaded.get("writable", []) + loaded.get("readonly", [])
    return out


def token_deltas(tx):
    """{(account_index, mint): (owner, delta_raw, decimals)} for every token account in the tx."""
    meta = tx.get("meta") or {}
    pre = {(b["accountIndex"], b["mint"]): b for b in meta.get("preTokenBalances") or []}
    post = {(b["accountIndex"], b["mint"]): b for b in meta.get("postTokenBalances") or []}
    out = {}
    for k in set(pre) | set(post):
        a, b = pre.get(k), post.get(k)
        amt = lambda x: int(x["uiTokenAmount"]["amount"]) if x else 0
        ref = b or a
        out[k] = (ref.get("owner"), amt(b) - amt(a), int(ref["uiTokenAmount"]["decimals"]))
    return out


# ---------------------------------------------------------------------------
# 1. agent wallet
# ---------------------------------------------------------------------------
def parse_agent_tx(tx, agent, sig):
    keys = keys_of(tx)
    meta = tx.get("meta") or {}
    sol_delta = 0
    if agent in keys:
        i = keys.index(agent)
        sol_delta = meta["postBalances"][i] - meta["preBalances"][i]
    tok = {}
    for (idx, mint), (owner, d, dec) in token_deltas(tx).items():
        if owner != agent or d == 0:
            continue
        if mint == WSOL:            # wrapped SOL counts as SOL
            sol_delta += d
            continue
        t = tok.setdefault(mint, [0, dec])
        t[0] += d
    ev = {"ts": tx.get("blockTime") or NOW, "sig": sig, "sol": round(sol_delta / LAMPORTS, 6)}
    bought = [(m, v) for m, v in tok.items() if v[0] > 0]
    sold = [(m, v) for m, v in tok.items() if v[0] < 0]
    if bought:
        m, (d, dec) = max(bought, key=lambda x: x[1][0])
        ev.update(kind="buy", mint=m, tokens=d / 10 ** dec)
    elif sold:
        m, (d, dec) = min(sold, key=lambda x: x[1][0])
        ev.update(kind="sell", mint=m, tokens=d / 10 ** dec)
    elif sol_delta > 0:
        ev.update(kind="fund")
    elif sol_delta < 0 and abs(sol_delta) > 100_000:  # bigger than a fee: a withdrawal / transfer out
        ev.update(kind="withdraw")
    else:
        return None
    return ev


def update_agent(cfg, state):
    agent = cfg.get("agent_wallet", "").strip()
    if not agent:
        return None
    st = state.setdefault("agent", {"last_sig": None, "events": [], "apes": 0, "spent": 0.0, "funded": 0.0})
    for s in new_signatures(agent, st["last_sig"], FIRST_RUN_AGENT):
        if out_of_time():
            break
        tx = None if s.get("err") else get_tx(s["signature"])
        if tx:
            ev = parse_agent_tx(tx, agent, s["signature"])
            if ev:
                st["events"].append(ev)
                if ev["kind"] == "buy":
                    st["apes"] += 1
                    st["spent"] += max(0.0, -ev["sol"])
                elif ev["kind"] == "fund":
                    st["funded"] += ev["sol"]
        st["last_sig"] = s["signature"]
    st["events"] = st["events"][-MAX_EVENTS_KEPT:]
    bal = (rpc("getBalance", [agent, {"commitment": "confirmed"}]) or {}).get("value", 0) / LAMPORTS
    return {"address": agent, "sol": round(bal, 4), "start_sol": cfg.get("start_sol", 1.0), "apes": st["apes"], "spent_sol": round(st["spent"], 4),
            "funded_sol": round(st["funded"], 4), "events": list(reversed(st["events"][-MAX_EVENTS_PUBLIC:]))}


# ---------------------------------------------------------------------------
# 2. $LMAO paid to holders (Pump.fun holder rewards vault)
# ---------------------------------------------------------------------------
def update_rewards(cfg, state):
    vault, lmao = cfg.get("reward_vault", "").strip(), cfg.get("lmao_mint", "").strip()
    if not vault or not lmao:
        return {"enabled": False}
    st = state.setdefault("rewards", {"last_sig": None, "total_raw": 0, "inflow_raw": 0, "decimals": 6,
                                      "drops": [], "wallets": []})
    wallets = set(st["wallets"])
    for s in new_signatures(vault, st["last_sig"], FIRST_RUN_VAULT):
        if out_of_time():
            break
        tx = None if s.get("err") else get_tx(s["signature"])
        if tx:
            keys = keys_of(tx)
            paid, got = 0, set()
            for (idx, mint), (owner, d, dec) in token_deltas(tx).items():
                if mint != lmao:
                    continue
                st["decimals"] = dec
                is_vault = keys[idx] == vault or owner == vault
                if is_vault:
                    if d < 0:
                        paid += -d
                    else:
                        st["inflow_raw"] += d
                elif d > 0:
                    got.add(owner)
            if paid > 0:
                ts = tx.get("blockTime") or NOW
                st["total_raw"] += paid
                wallets |= got
                drops = st["drops"]
                if drops and ts - drops[-1]["ts_last"] < DROP_GAP_SECONDS:
                    drops[-1]["raw"] += paid
                    drops[-1]["ts_last"] = ts
                    drops[-1]["wallets"] = len(set(drops[-1].get("_w", [])) | got)
                    drops[-1]["_w"] = sorted(set(drops[-1].get("_w", [])) | got)
                else:
                    drops.append({"ts": ts, "ts_last": ts, "raw": paid, "wallets": len(got), "_w": sorted(got)})
                for d in drops[:-1]:
                    d.pop("_w", None)      # only the open drop needs its wallet list
                st["drops"] = drops[-120:]
        st["last_sig"] = s["signature"]
    st["wallets"] = sorted(wallets)
    dec = st["decimals"]
    vault_bal = None
    try:
        acct = rpc("getTokenAccountBalance", [vault, {"commitment": "confirmed"}])
        vault_bal = float(acct["value"]["uiAmountString"])
    except Exception:
        # vault may be an owner wallet rather than a token account
        try:
            res = rpc("getTokenAccountsByOwner", [vault, {"mint": lmao}, {"encoding": "jsonParsed"}])
            vault_bal = sum(float(a["account"]["data"]["parsed"]["info"]["tokenAmount"]["uiAmountString"]) for a in res["value"])
        except Exception:
            pass
    drops = st["drops"]
    return {"enabled": True, "vault": vault, "total_lmao": st["total_raw"] / 10 ** dec,
            "fees_in_lmao": st["inflow_raw"] / 10 ** dec, "vault_lmao": vault_bal,
            "drops": len(drops), "wallets_paid": len(wallets),
            "last_drop": ({"ts": drops[-1]["ts_last"], "lmao": drops[-1]["raw"] / 10 ** dec, "wallets": drops[-1]["wallets"]} if drops else None),
            "history": [{"ts": d["ts"], "lmao": d["raw"] / 10 ** dec, "wallets": d["wallets"]} for d in drops[-30:]]}


# ---------------------------------------------------------------------------
# token names and logos for the agent's buys
# ---------------------------------------------------------------------------
def token_meta(mints, state):
    cache = state.setdefault("meta", {})
    need = [m for m in mints if m not in cache][:100]
    if need and env("HELIUS_API_KEY"):
        try:
            res = rpc("getAssetBatch", {"ids": need}) or []
            for a in res:
                if not a:
                    continue
                c = a.get("content") or {}
                md = c.get("metadata") or {}
                img = (c.get("links") or {}).get("image") or next((f.get("cdn_uri") or f.get("uri") for f in c.get("files") or [] if f), None)
                cache[a["id"]] = {"name": md.get("name") or "", "symbol": md.get("symbol") or "", "image": img or ""}
        except Exception as e:
            print("asset lookup failed:", e)
    need = [m for m in need if m not in cache]
    for i in range(0, len(need), 30):
        try:
            pairs = http_json("https://api.dexscreener.com/tokens/v1/solana/" + ",".join(need[i:i + 30])) or []
            for p in pairs:
                b = p.get("baseToken") or {}
                if b.get("address") in need and b["address"] not in cache:
                    cache[b["address"]] = {"name": b.get("name", ""), "symbol": b.get("symbol", ""), "image": (p.get("info") or {}).get("imageUrl", "")}
        except Exception as e:
            print("dexscreener lookup failed:", e)
    return cache


def main():
    cfg = json.loads(CONFIG.read_text())
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    agent = rewards = None
    try:
        agent = update_agent(cfg, state)
    except Exception as e:
        print("agent update failed:", e)
    try:
        rewards = update_rewards(cfg, state)
    except Exception as e:
        print("rewards update failed:", e)
    old = json.loads(DATA.read_text()) if DATA.exists() else {}
    if agent:
        meta = token_meta({e["mint"] for e in agent["events"] if e.get("mint")}, state)
        for e in agent["events"]:
            if e.get("mint"):
                e.update(meta.get(e["mint"], {}))
    data = {"updated": NOW, "updated_iso": datetime.fromtimestamp(NOW, timezone.utc).isoformat(timespec="seconds"),
            "agent": agent if agent is not None else old.get("agent"),
            "rewards": rewards if rewards is not None else old.get("rewards", {"enabled": False})}
    DATA.write_text(json.dumps(data, indent=1))
    STATE.write_text(json.dumps(state, separators=(",", ":")))
    a = data["agent"] or {}
    print(f"agent: {a.get('sol')} SOL, {a.get('apes')} apes | rewards: {json.dumps({k: v for k, v in (data['rewards'] or {}).items() if k != 'history'})}")
    print(f"done in {time.monotonic() - START:.1f}s")


if __name__ == "__main__":
    main()
