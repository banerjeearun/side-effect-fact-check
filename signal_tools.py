"""Side Effect Fact-Check tools: do side-effect reports back up a claim about a drug?

All data comes from openFDA (keyless). Verified live from a laptop:
  - plain queries return the match count in meta.results.total
  - a no-search query returns the total number of reports
  - the .exact count-by-reaction query and the drug label endpoint

Every tool returns a plain dict and never raises. Failures return
{"error": CODE, "hint": "what the model should do next"}.
Optional: set OPENFDA_API_KEY (free) to raise the daily limit from 1,000 to 120,000.
"""
from __future__ import annotations

import math
import os
import re
import time
from typing import Any

import requests

EVENT_URL = "https://api.fda.gov/drug/event.json"
LABEL_URL = "https://api.fda.gov/drug/label.json"
TIMEOUT_S = 20
CACHE_TTL_S = 6 * 3600

CAVEATS = [
    "Reports are voluntary and unverified, and a report does not prove the drug caused the reaction.",
    "A high ratio means a reaction is reported disproportionately, not that it is likely to happen to you.",
    "This is not medical advice. Never start or stop a medicine without talking to a doctor or pharmacist.",
]

_cache: dict[str, tuple[float, Any]] = {}


def _clean(term: str) -> str:
    """Keep only characters safe to put inside a quoted openFDA search term."""
    return re.sub(r'[^A-Za-z0-9 \-\+,/\.]', "", str(term or "")).strip()[:80]


def _get(url: str, params: dict) -> tuple[Any, dict | None]:
    """GET with one retry on transient failures. 404 is returned as ({}, None): openFDA uses it for 'no matches'."""
    key = os.environ.get("OPENFDA_API_KEY")
    if key:
        params = {**params, "api_key": key}
    ck = url + repr(sorted(params.items()))
    hit = _cache.get(ck)
    if hit and hit[0] > time.time():
        return hit[1], None
    last: dict | None = None
    for _ in range(2):
        try:
            r = requests.get(url, params=params, timeout=TIMEOUT_S)
        except requests.Timeout:
            last = {"error": "UPSTREAM_TIMEOUT", "hint": "openFDA was slow. Try again in a moment."}
            continue
        except requests.RequestException as exc:
            last = {"error": "UPSTREAM_ERROR", "hint": f"Network problem reaching openFDA: {exc.__class__.__name__}."}
            continue
        if r.status_code == 404:
            _cache[ck] = (time.time() + CACHE_TTL_S, {})
            return {}, None
        if r.status_code == 429:
            last = {"error": "RATE_LIMITED", "hint": "openFDA rate limit hit. Ask the user to wait a minute and retry."}
            time.sleep(1.5)
            continue
        if r.status_code == 200:
            try:
                data = r.json()
            except ValueError:
                return None, {"error": "UPSTREAM_BAD_JSON", "hint": "openFDA returned something unreadable."}
            _cache[ck] = (time.time() + CACHE_TTL_S, data)
            return data, None
        last = {"error": "UPSTREAM_ERROR", "hint": f"openFDA returned HTTP {r.status_code}."}
        if r.status_code < 500:
            break
    return None, last or {"error": "UPSTREAM_ERROR", "hint": "Unknown failure."}


def _total(search: str | None) -> tuple[int | None, dict | None]:
    params: dict[str, Any] = {"limit": 1}
    if search:
        params["search"] = search
    data, err = _get(EVENT_URL, params)
    if err:
        return None, err
    return int(((data or {}).get("meta", {}).get("results", {}) or {}).get("total", 0)), None


def _drug_q(drug: str) -> str:
    return f'patient.drug.openfda.generic_name:"{drug}"'


def _rx_q(reaction: str) -> str:
    return f'patient.reaction.reactionmeddrapt:"{reaction}"'


# ------------------------------------------------------------ pure math (unit tested)

def reporting_odds_ratio_math(a: int, drug_total: int, reaction_total: int, n_total: int, z: float = 1.96) -> dict:
    b, c = drug_total - a, reaction_total - a
    d = n_total - a - b - c
    if min(a, b, c, d) < 0:
        return {"error": "INCONSISTENT_COUNTS", "hint": "Counts did not add up; try again or use a different drug name."}
    if 0 in (a, b, c, d):
        return {"error": "ZERO_CELL", "hint": "Too few reports to compute a ratio for this pair."}
    ror = (a * d) / (b * c)
    se = math.sqrt(1 / a + 1 / b + 1 / c + 1 / d)
    return {"a": a, "b": b, "c": c, "d": d, "ror": ror,
            "ci_low": math.exp(math.log(ror) - z * se), "ci_high": math.exp(math.log(ror) + z * se)}


def reading(m: dict) -> str:
    if m["a"] < 5:
        return "Fewer than 5 matching reports, so the ratio is unreliable."
    if m["ci_low"] > 1:
        return "Reported more often with this drug than with other drugs."
    if m["ci_high"] < 1:
        return "Reported less often with this drug than with other drugs."
    return "No clear difference from other drugs."


# ------------------------------------------------------------ tools

def top_reactions(drug: str, limit: int = 10) -> dict:
    """Most frequently reported reactions for a drug (counts of reports, not rates)."""
    drug = _clean(drug).lower()
    if not drug:
        return {"error": "MISSING_DRUG", "hint": "Ask the user which drug they mean (generic name, e.g. 'ibuprofen')."}
    data, err = _get(EVENT_URL, {"search": _drug_q(drug), "count": "patient.reaction.reactionmeddrapt.exact",
                                 "limit": max(1, min(int(limit), 25))})
    if err:
        return err
    results = (data or {}).get("results") or []
    if not results:
        return {"error": "DRUG_NOT_FOUND", "hint": f"No reports found for '{drug}'. Try the generic name (e.g. 'acetaminophen' not 'Tylenol')."}
    return {"drug": drug, "top_reactions": [{"reaction": r["term"], "reports": r["count"]} for r in results],
            "note": "Counts are numbers of reports. Popular drugs have more reports of everything.", "caveats": CAVEATS[:1]}


def reporting_odds_ratio(drug: str, reaction: str) -> dict:
    """ORIGINAL TOOL: how disproportionately is this reaction reported with this drug versus all other drugs?"""
    drug, reaction = _clean(drug).lower(), _clean(reaction).lower()
    if not drug or not reaction:
        return {"error": "MISSING_INPUT", "hint": "Need both a drug (generic name) and a reaction (medical term, e.g. 'nausea')."}
    a, err = _total(f"{_drug_q(drug)} AND {_rx_q(reaction)}")
    if err: return err
    drug_total, err = _total(_drug_q(drug))
    if err: return err
    if drug_total == 0:
        return {"error": "DRUG_NOT_FOUND", "hint": f"No reports for '{drug}'. Try the generic name."}
    reaction_total, err = _total(_rx_q(reaction))
    if err: return err
    if reaction_total == 0:
        return {"error": "REACTION_NOT_FOUND", "hint": f"'{reaction}' is not a recognised reaction term. Call top_reactions to see valid terms, or use a medical term like 'nausea' or 'headache'."}
    n_total, err = _total(None)
    if err: return err
    m = reporting_odds_ratio_math(a, drug_total, reaction_total, n_total)
    if "error" in m:
        return m
    return {"drug": drug, "reaction": reaction, "reports_with_drug_and_reaction": a, "reports_with_drug": drug_total,
            "reports_with_reaction": reaction_total, "all_reports": n_total,
            "reporting_odds_ratio": round(m["ror"], 2),
            "ci_95": [round(m["ci_low"], 2), round(m["ci_high"], 2)],
            "reading": reading(m), "caveats": CAVEATS}


_LABEL_SECTIONS = ("boxed_warning", "warnings", "warnings_and_cautions", "adverse_reactions", "precautions")


def _label_rank(label: dict, drug: str) -> tuple[bool, bool, bool]:
    """Prefer a single-ingredient label, then an oral one, then one with an adverse reactions section.

    Verified live: the first match is often an OTC 'Drug Facts' label (no adverse reactions),
    a combination product, or eye drops (ciprofloxacin), which hid well-known reactions.
    """
    openfda = label.get("openfda", {})
    names = [n.lower() for n in openfda.get("generic_name", [])]
    single = any(n.startswith(drug) and " and " not in n for n in names)
    oral = "ORAL" in openfda.get("route", [])
    return single, oral, "adverse_reactions" in label


def _label(drug: str) -> tuple[dict | None, dict | None]:
    data, err = _get(LABEL_URL, {"search": f'openfda.generic_name:"{drug}"', "limit": 25})
    if err:
        return None, err
    results = (data or {}).get("results") or []
    if not results:
        return None, {"error": "NO_LABEL", "hint": f"No FDA label found for '{drug}'. Try the generic name."}
    return max(results, key=lambda l: _label_rank(l, drug)), None


def _section_text(label: dict, name: str) -> str:
    v = label.get(name)
    return " ".join(v) if isinstance(v, list) else (v or "")


def get_label_warnings(drug: str) -> dict:
    """What the official FDA label warns about (boxed warning, warnings, adverse reactions), truncated."""
    drug = _clean(drug).lower()
    if not drug:
        return {"error": "MISSING_DRUG", "hint": "Ask the user which drug they mean."}
    label, err = _label(drug)
    if err: return err
    out: dict[str, Any] = {"drug": drug}
    for s in _LABEL_SECTIONS:
        t = _section_text(label, s)
        if t:
            out[s] = t[:1200] + ("..." if len(t) > 1200 else "")
    if len(out) == 1:
        return {"error": "LABEL_EMPTY", "hint": "The label has no warning sections in the structured data."}
    out["caveats"] = CAVEATS[2:]
    return out


def _spellings(reaction: str) -> list[str]:
    """Report terms are MedDRA (British: 'oedema peripheral'); US labels say 'peripheral edema'."""
    us = reaction.replace("haem", "hem").replace("oe", "e").replace("ae", "e")
    out = [reaction, us]
    words = us.split()
    if len(words) == 2:
        out.append(f"{words[1]} {words[0]}")
    return list(dict.fromkeys(out))


def check_label_for_reaction(drug: str, reaction: str) -> dict:
    """Does the official label mention this reaction? Text match (with US spellings), so synonyms can be missed."""
    drug, reaction = _clean(drug).lower(), _clean(reaction).lower()
    if not drug or not reaction:
        return {"error": "MISSING_INPUT", "hint": "Need both a drug and a reaction term."}
    label, err = _label(drug)
    if err: return err
    found = []
    snippet = None
    terms = _spellings(reaction)
    for s in _LABEL_SECTIONS:
        t = _section_text(label, s)
        low = t.lower()
        i = next((i for i in (low.find(term) for term in terms) if i >= 0), -1)
        if i >= 0:
            found.append(s)
            snippet = snippet or t[max(0, i - 120): i + 160].strip()
    return {"drug": drug, "reaction": reaction, "on_label": bool(found), "sections": found, "snippet": snippet,
            "note": "Text match only: a synonym on the label would not be detected."}


def assess_signal(drug: str, reaction: str) -> dict:
    """ORIGINAL TOOL: combine reporting ratio and label check into 'known effect' vs 'worth asking about'."""
    ror = reporting_odds_ratio(drug, reaction)
    if "error" in ror: return ror
    lab = check_label_for_reaction(drug, reaction)
    if "error" in lab: return {**ror, "label_check": lab}
    disproportionate = ror["ci_95"][0] > 1 and ror["reports_with_drug_and_reaction"] >= 5
    if disproportionate and lab["on_label"]:
        verdict = "known_effect"
        meaning = "Reported disproportionately and already listed on the FDA label."
    elif disproportionate:
        verdict = "not_on_label_worth_asking_about"
        meaning = ("Reported disproportionately but no matching text on the label. The label may use different wording, "
                   "so this is a prompt for a question to a pharmacist, not a finding.")
    elif lab["on_label"]:
        verdict = "on_label_not_disproportionate"
        meaning = "Listed on the label, but not reported unusually often compared with other drugs."
    else:
        verdict = "no_signal"
        meaning = "Not reported disproportionately and not found on the label."
    return {"drug": ror["drug"], "reaction": ror["reaction"], "verdict": verdict, "meaning": meaning,
            "reporting_odds_ratio": ror["reporting_odds_ratio"], "ci_95": ror["ci_95"],
            "reports_with_drug_and_reaction": ror["reports_with_drug_and_reaction"],
            "label": {"on_label": lab["on_label"], "sections": lab["sections"], "snippet": lab["snippet"]},
            "caveats": CAVEATS}


TOOLS = {f.__name__: f for f in (top_reactions, reporting_odds_ratio, get_label_warnings,
                                 check_label_for_reaction, assess_signal)}

_DRUG = {"type": "string", "description": "Generic drug name in plain English, e.g. 'ibuprofen' (not a brand like Advil)."}
_RX = {"type": "string", "description": "Reaction as a medical term, e.g. 'nausea', 'headache', 'dizziness'."}
TOOL_SPECS = [
    {"name": "top_reactions",
     "description": "List the reactions most often reported for a drug in the FDA adverse event database. Use it to find valid reaction terms or to answer 'what do people report for X'.",
     "parameters": {"type": "object", "properties": {"drug": _DRUG, "limit": {"type": "integer", "description": "How many reactions, 1-25. Default 10."}}, "required": ["drug"]}},
    {"name": "reporting_odds_ratio",
     "description": "Compute how disproportionately a reaction is reported with a drug compared with all other drugs. Returns a ratio with a 95% interval. A ratio above 1 with an interval above 1 means over-reported, not proven caused.",
     "parameters": {"type": "object", "properties": {"drug": _DRUG, "reaction": _RX}, "required": ["drug", "reaction"]}},
    {"name": "get_label_warnings",
     "description": "Fetch what the official FDA label says: boxed warning, warnings and adverse reactions (truncated).",
     "parameters": {"type": "object", "properties": {"drug": _DRUG}, "required": ["drug"]}},
    {"name": "check_label_for_reaction",
     "description": "Check whether the official FDA label mentions a specific reaction, with a text snippet. Text match only.",
     "parameters": {"type": "object", "properties": {"drug": _DRUG, "reaction": _RX}, "required": ["drug", "reaction"]}},
    {"name": "assess_signal",
     "description": "Best tool for 'does drug X really cause Y?'. Combines the reporting ratio with the label check and returns known_effect, not_on_label_worth_asking_about, on_label_not_disproportionate or no_signal. Always relay the caveats.",
     "parameters": {"type": "object", "properties": {"drug": _DRUG, "reaction": _RX}, "required": ["drug", "reaction"]}},
]


def run_tool(name: str, args: dict) -> dict:
    fn = TOOLS.get(name)
    if not fn:
        return {"error": "UNKNOWN_TOOL", "hint": f"Available tools: {', '.join(TOOLS)}."}
    try:
        return fn(**(args or {}))
    except TypeError as exc:
        return {"error": "BAD_ARGUMENTS", "hint": f"Check argument names and types: {exc}"}
    except Exception as exc:
        return {"error": "TOOL_CRASH", "hint": f"Unexpected failure ({exc.__class__.__name__}). Tell the user and try another drug or reaction."}
