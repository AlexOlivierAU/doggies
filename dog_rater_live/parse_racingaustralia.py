from __future__ import annotations

import logging
import re
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from typing import Optional
from urllib.parse import urljoin, urlparse, parse_qs

from bs4 import BeautifulSoup

from fetch import FetchError, get
from models import Meeting, Race, Runner
from services.runner_numbers import parse_program_number_cell, program_number_from_raw


def official_program_number(headers: list, cells: list) -> Optional[int]:
    """Official saddle/program number from an Acceptances (or similar) row.

    Never returns the barrier/draw. Returns None when the No column is absent
    and the horse cell has no leading cloth number.
    """
    hlow = [str(h).strip().lower() for h in (headers or [])]

    def idx(*names: str) -> Optional[int]:
        for nm in names:
            if nm.lower() in hlow:
                return hlow.index(nm.lower())
        return None

    i_no = idx("no", "no.", "number", "#", "saddle", "program", "cloth")
    i_barrier = idx("barrier")
    if i_no is not None and i_no < len(cells or []) and i_no != i_barrier:
        n = parse_program_number_cell(cells[i_no])
        if n is not None:
            return n
    return program_number_from_raw(headers, cells)


log = logging.getLogger("race_day_rater.calendar")

BASE = "https://www.racingaustralia.horse"
CAL_URL = "https://www.racingaustralia.horse/FreeFields/Calendar.aspx?State={state}"
CALENDAR_STATES = ("NSW", "VIC", "QLD", "WA", "SA", "TAS", "ACT", "NT")


class MeetingList(list):
    """list[Meeting] plus calendar diagnostics. Streamlit can iterate it as a list."""

    failed_states: list[str]
    failed_details: list[str]

    def __init__(self, meetings=(), *, failed_states=None, failed_details=None):
        super().__init__(meetings)
        self.failed_states = list(failed_states or [])
        self.failed_details = list(failed_details or [])

# Key shape: 2025Dec06,WA,Ascot
_KEY_RE = re.compile(r"^\s*(\d{4})([A-Za-z]{3})(\d{2})\s*$")
_TIME_12H_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})(AM|PM)\s*$", re.IGNORECASE)
_RACE_LINE_RE = re.compile(
    r"Race\s+(?P<no>\d+)\s*-\s*(?P<t>\d{1,2}:\d{2}\s*(?:AM|PM))\s+(?P<name>.+?)\s*\((?P<dist>\d{3,4})\s*METRE",
    re.IGNORECASE,
)
_COUNTRY_SUFFIX_RE = re.compile(r"\s*\(([A-Z]{2,3}|NZ|GB|IRE|USA|FR|JPN|GER|ITY)\)\s*$", re.IGNORECASE)

# Rough numeric ladder for class-up / class-down comparisons (higher = tougher).
_CLASS_RANK = {
    "Trial": 0,
    "MDN": 10,
    "Cl1": 20,
    "Cl2": 25,
    "Cl3": 30,
    "Cl4": 35,
    "Cl5": 40,
    "Cl6": 45,
    "OPEN": 70,
    "LR": 85,
    "G3": 90,
    "G2": 95,
    "G1": 100,
}


def parse_race_class_label(name: str) -> str:
    """
    Short AU race-class label from a race title or Form last-start line.

    Examples: "BENCHMARK 58 HANDICAP" -> "BM58", "CLASS 1 PLATE" -> "Cl1",
    "MAIDEN" / "MDN-SW" -> "MDN", "GROUP 1" -> "G1", "BM62" -> "BM62".
    """
    s = (name or "").upper()
    if not s.strip():
        return ""
    m = re.search(r"\bGROUP\s*([123])\b", s)
    if m:
        return f"G{m.group(1)}"
    if re.search(r"\bLISTED\b|\bLR\b", s):
        return "LR"
    m = re.search(r"\bBENCH(?:MARK)?\s*(\d+)\b", s)
    if m:
        return f"BM{m.group(1)}"
    m = re.search(r"\bBM\s*(\d+)\b", s)
    if m:
        return f"BM{m.group(1)}"
    m = re.search(r"\bCLASS\s*([1-6])\b", s)
    if m:
        return f"Cl{m.group(1)}"
    # Form abbreviations: CL1, CL2-SW, etc.
    m = re.search(r"\bCL\s*([1-6])\b", s)
    if m:
        return f"Cl{m.group(1)}"
    if re.search(r"\bMAIDEN\b|\bMDN\b", s):
        return "MDN"
    if re.search(r"\bTRIAL\b|\bTRL\b", s):
        return "Trial"
    if re.search(r"\bOPEN\b", s):
        return "OPEN"
    return ""


def parse_last_start_class(remain_text: str) -> str:
    """
    Class label from a Form.aspx horse-last-start remain cell.
    Skips jump-outs; returns "" when unknown.
    """
    t = (remain_text or "").strip()
    if not t:
        return ""
    if re.search(r"jump\s*out|jumpout", t, re.IGNORECASE):
        return ""
    return parse_race_class_label(t)


def class_rank_value(label: str) -> Optional[float]:
    """Numeric class strength for up/down comparisons. BM58 -> 58; Cl3 -> mapped ladder."""
    lab = (label or "").strip()
    if not lab:
        return None
    m = re.match(r"^BM(\d+)$", lab, re.IGNORECASE)
    if m:
        return float(m.group(1))
    # Normalize Cl1 / CL1
    m = re.match(r"^CL\s*([1-6])$", lab, re.IGNORECASE)
    if m:
        lab = f"Cl{m.group(1)}"
    return float(_CLASS_RANK[lab]) if lab in _CLASS_RANK else None


def class_change_arrow(prev_label: str, today_label: str) -> str:
    """
    Return ↑ / ↓ / → for class move (today vs previous race), or "" if unknown.
    ↑ = stepping up in class; ↓ = dropping back.
    """
    a = class_rank_value(prev_label)
    b = class_rank_value(today_label)
    if a is None or b is None:
        return ""
    if b > a + 0.5:
        return "↑"
    if b < a - 0.5:
        return "↓"
    return "→"


def runner_last_class(r: Runner) -> str:
    return str(((getattr(r, "raw", None) or {}) or {}).get("last_class") or "").strip()


def runner_class_arrow(r: Runner, today_label: str) -> str:
    return class_change_arrow(runner_last_class(r), today_label)


class ParseError(RuntimeError):
    pass


def _parse_key_date(s: str) -> Optional[date]:
    m = _KEY_RE.match((s or "").strip())
    if not m:
        return None
    y = int(m.group(1))
    mon = m.group(2).lower()
    d = int(m.group(3))
    months = {
        "jan": 1,
        "feb": 2,
        "mar": 3,
        "apr": 4,
        "may": 5,
        "jun": 6,
        "jul": 7,
        "aug": 8,
        "sep": 9,
        "oct": 10,
        "nov": 11,
        "dec": 12,
    }
    if mon not in months:
        return None
    return date(y, months[mon], d)


def _parse_time_12h(s: str) -> Optional[time]:
    m = _TIME_12H_RE.match((s or "").strip().replace(" ", ""))
    if not m:
        return None
    hh = int(m.group(1))
    mm = int(m.group(2))
    ap = m.group(3).upper()
    if hh == 12:
        hh = 0
    if ap == "PM":
        hh += 12
    if 0 <= hh <= 23 and 0 <= mm <= 59:
        return time(hour=hh, minute=mm)
    return None


def _meeting_status(now_local: datetime, meeting_date: date, first_time: Optional[time], num_races: Optional[int]) -> str:
    if first_time is None:
        return "unknown"
    start_dt = datetime.combine(meeting_date, first_time, tzinfo=now_local.tzinfo)
    if now_local < start_dt:
        return "upcoming"
    races = num_races or 8
    est_end = start_dt + timedelta(minutes=races * 35)
    if now_local <= est_end:
        return "in_progress"
    return "finished"


def _key_from_url(url: str) -> Optional[str]:
    try:
        q = parse_qs(urlparse(url).query)
    except Exception:
        return None
    k = (q.get("Key") or [None])[0]
    return k


def _norm_horse_name(name: str) -> str:
    """Normalize horse names for matching Acceptances ↔ Form silks."""
    s = re.sub(r"\s+", " ", (name or "").strip()).upper()
    s = _COUNTRY_SUFFIX_RE.sub("", s).strip()
    return s


def _absolute_silk_url(src: str) -> str:
    if not src:
        return ""
    full = urljoin(BASE + "/", src)
    if full.startswith("http://"):
        full = "https://" + full[len("http://") :]
    return full


def _fetch_form_extras_by_horse(key: str, *, ttl_seconds: int) -> dict[str, dict[str, str]]:
    """
    Form.aspx: jockey silks + last-start class per horse.
    Returns normalized horse name -> {"silk_url": ..., "last_class": ...}.
    """
    form_url = f"{BASE}/FreeFields/Form.aspx?Key={key}"
    try:
        resp = get(form_url, ttl_seconds=ttl_seconds, timeout_seconds=45.0)
    except FetchError:
        return {}
    soup = BeautifulSoup(resp.text, "html.parser")
    out: dict[str, dict[str, str]] = {}

    # Prefer structured horse-form-table blocks (name + last starts together).
    for ft in soup.find_all(class_="horse-form-table"):
        name_el = ft.find(class_="horse-name")
        if name_el is None:
            continue
        horse_name = (name_el.get_text(" ", strip=True) or "").strip()
        norm = _norm_horse_name(horse_name)
        if not norm:
            continue
        entry = out.setdefault(norm, {})
        if "silk_url" not in entry:
            img = ft.find("img", src=re.compile(r"JockeySilks", re.IGNORECASE))
            if img and img.get("src"):
                silk = _absolute_silk_url(img.get("src") or "")
                if silk:
                    entry["silk_url"] = silk
        if "last_class" not in entry:
            ls = ft.find(class_="horse-last-start")
            if ls is not None:
                for tr in ls.find_all("tr"):
                    rem = tr.find(class_="remain")
                    if rem is None:
                        continue
                    lab = parse_last_start_class(rem.get_text(" ", strip=True) or "")
                    # Prefer a real race over a trial for class comparison.
                    if lab and lab != "Trial":
                        entry["last_class"] = lab
                        break
                if "last_class" not in entry and ls is not None:
                    for tr in ls.find_all("tr"):
                        rem = tr.find(class_="remain")
                        if rem is None:
                            continue
                        lab = parse_last_start_class(rem.get_text(" ", strip=True) or "")
                        if lab:
                            entry["last_class"] = lab
                            break

    # Fallback silk scrape for pages without horse-form-table layout.
    for img in soup.find_all("img"):
        src = img.get("src") or ""
        if "JockeySilks" not in src:
            continue
        silk = _absolute_silk_url(src)
        if not silk:
            continue
        cell = img.find_parent("td") or img.parent
        if cell is None:
            continue
        horse_name = None
        for a in cell.find_all("a", href=True):
            href = a.get("href") or ""
            if "HorseFullForm" not in href and "horseform" not in href.lower():
                continue
            horse_name = (a.get_text(" ", strip=True) or "").strip()
            break
        if not horse_name:
            continue
        norm = _norm_horse_name(horse_name)
        if not norm:
            continue
        entry = out.setdefault(norm, {})
        if "silk_url" not in entry:
            entry["silk_url"] = silk
    return out


def _fetch_silk_urls_by_horse(key: str, *, ttl_seconds: int) -> dict[str, str]:
    """Backward-compatible silk map from Form.aspx."""
    extras = _fetch_form_extras_by_horse(key, ttl_seconds=ttl_seconds)
    return {k: v["silk_url"] for k, v in extras.items() if v.get("silk_url")}


def _apply_form_extras_to_runners(
    runners_by_race: dict[int, list[Runner]], extras_by_horse: dict[str, dict[str, str]]
) -> dict[int, list[Runner]]:
    if not extras_by_horse:
        return runners_by_race
    enriched: dict[int, list[Runner]] = {}
    for race_no, runners in runners_by_race.items():
        updated: list[Runner] = []
        for r in runners:
            extra = extras_by_horse.get(_norm_horse_name(r.name)) or {}
            silk = extra.get("silk_url") or ""
            last_class = extra.get("last_class") or ""
            new_silk = getattr(r, "silk_url", None) or (silk or None)
            raw = dict(getattr(r, "raw", None) or {})
            if last_class and not raw.get("last_class"):
                raw["last_class"] = last_class
            if new_silk != getattr(r, "silk_url", None) or raw != (getattr(r, "raw", None) or {}):
                updated.append(replace(r, silk_url=new_silk, raw=raw))
            else:
                updated.append(r)
        enriched[race_no] = updated
    return enriched


def _apply_silks_to_runners(
    runners_by_race: dict[int, list[Runner]], silk_by_horse: dict[str, str]
) -> dict[int, list[Runner]]:
    extras = {k: {"silk_url": v} for k, v in (silk_by_horse or {}).items() if v}
    return _apply_form_extras_to_runners(runners_by_race, extras)


def runners_missing_silks(runners_by_race: dict[int, list[Runner]] | None) -> bool:
    """True when there are runners but none have a silk_url yet."""
    any_runner = False
    for runners in (runners_by_race or {}).values():
        for r in runners or []:
            any_runner = True
            if getattr(r, "silk_url", None):
                return False
    return any_runner


def runners_missing_last_class(runners_by_race: dict[int, list[Runner]] | None) -> bool:
    """True when there are runners but none have raw.last_class yet."""
    any_runner = False
    for runners in (runners_by_race or {}).values():
        for r in runners or []:
            if bool(getattr(r, "scratched", False)):
                continue
            any_runner = True
            if runner_last_class(r):
                return False
    return any_runner


def enrich_runners_with_silks(
    meeting_url: str,
    runners_by_race: dict[int, list[Runner]],
    *,
    ttl_seconds: int = 300,
    force: bool = False,
) -> dict[int, list[Runner]]:
    """
    Attach jockey silk URLs and last-start class from Form.aspx.
    No-op if silks + last_class already present (unless force=True) or Key missing.
    """
    if not runners_by_race:
        return runners_by_race
    need = force or runners_missing_silks(runners_by_race) or runners_missing_last_class(runners_by_race)
    if not need:
        return runners_by_race
    key = _key_from_url(meeting_url)
    if not key:
        return runners_by_race
    try:
        extras = _fetch_form_extras_by_horse(key, ttl_seconds=ttl_seconds)
    except Exception:
        return runners_by_race
    return _apply_form_extras_to_runners(runners_by_race, extras)


def _calendar_priority(href: str) -> Optional[int]:
    priorities = {
        "Acceptances.aspx?Key=": 4,
        "Form.aspx?Key=": 3,
        "AllForm.aspx?Key=": 3,
        "Weights.aspx?Key=": 2,
        "RaceProgram.aspx?Key=": 1,
        "Nominations.aspx?Key=": 0,
    }
    for frag, p in priorities.items():
        if frag in (href or ""):
            return p
    return None


def _collect_calendar_links(html_text: str, meeting_date: date, best_by_key: dict[str, tuple[int, str, str, str]]) -> None:
    soup = BeautifulSoup(html_text or "", "html.parser")
    for a in soup.find_all("a"):
        href = a.get("href") or ""
        pr = _calendar_priority(href)
        if pr is None:
            continue
        full = urljoin(BASE, href)
        key = _key_from_url(full)
        if not key:
            continue
        parts = [p.strip() for p in key.split(",") if p.strip()]
        if len(parts) < 3:
            continue
        d = _parse_key_date(parts[0])
        if d != meeting_date:
            continue
        venue = parts[2].strip()
        state = parts[1].strip()
        cur = best_by_key.get(key)
        if cur is None or pr > cur[0]:
            best_by_key[key] = (pr, full, venue, state)


def fetch_meetings_for_date(meeting_date: date, *, ttl_seconds: int = 300) -> list[Meeting]:
    """
    Fetch *all Australian* thoroughbred meetings for a given date using Racing Australia FreeFields.
    We scrape state calendar pages and collect Acceptances links (Key=YYYYMonDD,STATE,VENUE).

    Each state calendar is isolated: a timeout or parse failure for one state does not
    discard meetings already collected from the others. The returned object is a list
    (compatible with existing Streamlit callers) with `.failed_states` / `.failed_details`.
    """
    now_local = datetime.now().astimezone()
    best_by_key: dict[str, tuple[int, str, str, str]] = {}
    failed_states: list[str] = []
    failed_details: list[str] = []

    for st in CALENDAR_STATES:
        url = CAL_URL.format(state=st)
        try:
            resp = get(url, ttl_seconds=ttl_seconds)
            _collect_calendar_links(resp.text, meeting_date, best_by_key)
        except (FetchError, ParseError, OSError, TimeoutError) as exc:
            log.warning("Racing Australia calendar unavailable for %s: %s", st, exc)
            failed_states.append(st)
            failed_details.append(f"{st}: {exc}")
            continue
        except Exception as exc:
            log.exception("Racing Australia calendar failed for %s", st)
            failed_states.append(st)
            failed_details.append(f"{st}: {exc}")
            continue

    meetings: list[Meeting] = []
    for key, (_pr, full, venue, state) in best_by_key.items():
        meetings.append(
            Meeting(
                code="thoroughbred",
                source="racingaustralia",
                venue=venue,
                meeting_date=meeting_date,
                first_race_time_local=None,
                num_races=None,
                meeting_url=full,
                status=_meeting_status(now_local, meeting_date, None, None),
                extra={"state": state, "key": key},
            )
        )
    ordered = sorted(meetings, key=lambda m: (m.extra.get("state") or "", m.venue))
    if failed_states:
        log.warning(
            "Calendar partial: %s meetings from successful states; failed=%s",
            len(ordered),
            ",".join(failed_states),
        )
    return MeetingList(ordered, failed_states=failed_states, failed_details=failed_details)


def fetch_races_and_runners_for_meeting(meeting_url: str, *, ttl_seconds: int = 300) -> tuple[list[Race], dict[int, list[Runner]], dict]:
    """
    Parse races + runners from a Racing Australia Acceptances page.
    """
    key = _key_from_url(meeting_url)
    if not key:
        raise ParseError("Missing Key=... in meeting URL")

    # Prefer Acceptances page for runners (has barrier/jockey etc), even if the calendar linked a different page.
    # Fall back to the provided meeting_url (often Weights) if acceptances is unavailable.
    acceptances_url = f"{BASE}/FreeFields/Acceptances.aspx?Key={key}"
    soup = None
    try:
        resp = get(acceptances_url, ttl_seconds=ttl_seconds)
        soup = BeautifulSoup(resp.text, "html.parser")
    except FetchError:
        resp2 = get(meeting_url, ttl_seconds=ttl_seconds)
        soup = BeautifulSoup(resp2.text, "html.parser")

    # Race program: has race times even when weights/acceptances layouts omit them.
    program_url = f"{BASE}/FreeFields/RaceProgram.aspx?Key={key}"
    program_meta: dict[int, dict] = {}
    try:
        presp = get(program_url, ttl_seconds=ttl_seconds)
        ptext = BeautifulSoup(presp.text, "html.parser").get_text(" ", strip=True)
        for m in _RACE_LINE_RE.finditer(ptext):
            try:
                rn = int(m.group("no"))
            except Exception:
                continue
            program_meta[rn] = {
                "name": (m.group("name") or "").strip(),
                "distance_m": int(m.group("dist")),
                "start_time_local": _parse_time_12h(m.group("t")),
            }
    except Exception:
        program_meta = {}

    # Meta (best-effort)
    page_text = soup.get_text("\n", strip=True)
    meta: dict[str, str] = {}
    for label in ["Track Condition", "Weather", "Penetrometer"]:
        m = re.search(rf"{re.escape(label)}:\s*([^\n]+)", page_text, re.IGNORECASE)
        if m:
            meta_key = label.lower().replace(" ", "_")
            meta[meta_key] = m.group(1).strip()

    races: list[Race] = []
    runners_by_race: dict[int, list[Runner]] = {}

    # Race anchors are typically named Race1, Race2, ... Acceptances may only show upcoming (e.g. Race4+);
    # skip missing anchors so we still collect every race that appears on the page, then fill gaps below.
    for race_no in range(1, 25):
        anchor = soup.find(attrs={"name": f"Race{race_no}"}) or soup.find(id=f"Race{race_no}")
        if anchor is None:
            continue

        # Collect the section until the next race anchor.
        section_text = ""
        runner_table = None
        for el in anchor.next_elements:
            if getattr(el, "attrs", None):
                nm = el.attrs.get("name") or el.attrs.get("id")
                if isinstance(nm, str) and nm.startswith("Race") and nm != f"Race{race_no}":
                    break
            if getattr(el, "get_text", None):
                t = el.get_text(" ", strip=True)
                if t:
                    section_text += " " + t
            if getattr(el, "name", None) == "table" and runner_table is None:
                headers = [th.get_text(" ", strip=True) for th in el.find_all("th")]
                hlow = [h.strip().lower() for h in headers]
                # Acceptances pages: horse+barrier. Weights pages: horse+weight (no barrier).
                if "horse" in hlow and ("barrier" in hlow or "weight" in hlow or "true weight" in hlow):
                    runner_table = el
                    # Keep scanning a little more in case the race line appears after the table.

        # Prefer program meta; fall back to parsing the local section text.
        pm = program_meta.get(race_no) or {}
        m_race = _RACE_LINE_RE.search(section_text)
        race_name = (pm.get("name") or "").strip() or (m_race.group("name").strip() if m_race else f"Race {race_no}")
        dist_m = pm.get("distance_m")
        if dist_m is None and m_race:
            try:
                dist_m = int(m_race.group("dist"))
            except Exception:
                dist_m = None
        start_t = pm.get("start_time_local")
        if start_t is None and m_race:
            start_t = _parse_time_12h(m_race.group("t"))

        race_url = meeting_url.split("#", 1)[0] + f"#Race{race_no}"
        races.append(
            Race(
                code="thoroughbred",
                race_no=race_no,
                name=race_name,
                distance_m=dist_m,
                start_time_local=start_t,
                race_url=race_url,
                extra={"class_label": parse_race_class_label(race_name)},
            )
        )

        runners: list[Runner] = []
        if runner_table is not None:
            headers = [th.get_text(" ", strip=True) for th in runner_table.find_all("th")]
            hlow = [h.strip().lower() for h in headers]

            def idx(*names: str) -> Optional[int]:
                for nm in names:
                    nm = nm.lower()
                    if nm in hlow:
                        return hlow.index(nm)
                return None

            i_last10 = idx("last 10", "last10", "last 5")
            i_horse = idx("horse", "runner")
            i_sex = idx("sex")
            i_age = idx("age")
            i_trainer = idx("trainer")
            i_jockey = idx("jockey", "rider")
            i_barrier = idx("barrier")
            # Some pages (e.g. Weights) have no barrier; keep draw=None in that case.
            i_weight = idx("weight")
            i_hcp = idx("hcp rating", "rating", "handicap rating")
            i_status = idx("status", "scr", "scratching")

            for tr in runner_table.find_all("tr"):
                tds = tr.find_all("td")
                if not tds:
                    continue
                cells = [td.get_text(" ", strip=True) for td in tds]
                if i_horse is None or i_horse >= len(cells):
                    continue

                horse_name = cells[i_horse].strip()
                if not horse_name:
                    continue

                # Detect scratched via status column or whole-word SCR/SCRATCHED
                # (avoid false positives on names like SCRUFFY / SCRUFFETTE).
                row_text = " ".join(cells).upper()
                status_cell = (cells[i_status].strip().upper() if i_status is not None and i_status < len(cells) else "") or ""
                is_scratched = (
                    status_cell in ("SCR", "SCRATCHED", "S")
                    or bool(re.search(r"\bSCRATCHED\b", row_text))
                    or bool(re.search(r"\bSCR\b", row_text))
                )

                # profile link (horse full form)
                prof = None
                try:
                    a = tds[i_horse].find("a")
                    if a and a.get("href"):
                        prof = urljoin(meeting_url, a.get("href"))
                except Exception:
                    prof = None

                last10 = cells[i_last10].strip() if (i_last10 is not None and i_last10 < len(cells)) else ""
                finishes: list[int] = []
                for ch in last10:
                    if ch.isdigit():
                        v = int(ch)
                        finishes.append(10 if v == 0 else v)

                sex = cells[i_sex].strip() if (i_sex is not None and i_sex < len(cells)) else None
                age = None
                if i_age is not None and i_age < len(cells):
                    try:
                        age = int(re.sub(r"[^\d]", "", cells[i_age]) or "")
                    except Exception:
                        age = None
                # barrier (draw) — distinct from official program/saddle number
                draw = None
                if i_barrier is not None and i_barrier < len(cells):
                    try:
                        draw = int(re.sub(r"[^\d]", "", cells[i_barrier]) or "0") or None
                    except Exception:
                        draw = None
                program_number = official_program_number(headers, cells)
                # weight
                wt = None
                if i_weight is not None and i_weight < len(cells):
                    mwt = re.search(r"(\d+(?:\.\d+)?)\s*kg", cells[i_weight], re.IGNORECASE)
                    if mwt:
                        try:
                            wt = float(mwt.group(1))
                        except Exception:
                            wt = None
                # benchmark/rating
                bm = None
                if i_hcp is not None and i_hcp < len(cells):
                    try:
                        bm = float(re.sub(r"[^\d.]", "", cells[i_hcp]) or "")
                    except Exception:
                        bm = None

                trainer = cells[i_trainer].strip() if (i_trainer is not None and i_trainer < len(cells)) else None
                jockey = cells[i_jockey].strip() if (i_jockey is not None and i_jockey < len(cells)) else None

                runners.append(
                    Runner(
                        code="thoroughbred",
                        name=horse_name,
                        draw=draw,
                        recent_finishes=finishes,
                        early_speed=None,
                        age=age,
                        sex=sex,
                        profile_url=prof,
                        weight_kg=wt,
                        benchmark=bm,
                        trainer=trainer,
                        jockey_or_driver=jockey,
                        last10=last10 or None,
                        scratched=is_scratched,
                        raw={"headers": headers, "cells": cells, "program_number": program_number},
                        program_number=program_number,
                    )
                )

        runners_by_race[race_no] = runners

    # Acceptances often only shows upcoming races; RaceProgram has the full card. Add any races from program we don't have.
    if program_meta:
        existing_nos = {r.race_no for r in races}
        for race_no in sorted(program_meta.keys()):
            if race_no in existing_nos:
                continue
            pm = program_meta[race_no]
            race_name = (pm.get("name") or "").strip() or f"Race {race_no}"
            races.append(
                Race(
                    code="thoroughbred",
                    race_no=race_no,
                    name=race_name,
                    distance_m=pm.get("distance_m"),
                    start_time_local=pm.get("start_time_local"),
                    race_url=(meeting_url.split("#", 1)[0] + f"#Race{race_no}"),
                    extra={"class_label": parse_race_class_label(race_name)},
                )
            )
            runners_by_race[race_no] = []
        races.sort(key=lambda r: r.race_no)

    # If we have any races (e.g. R4, R5 from acceptances) but are missing earlier numbers, fill gaps so grid shows full card.
    if races:
        existing_nos = {r.race_no for r in races}
        max_no = max(existing_nos)
        for race_no in range(1, max_no):
            if race_no in existing_nos:
                continue
            pm = program_meta.get(race_no) or {}
            race_name = (pm.get("name") or "").strip() or f"Race {race_no}"
            races.append(
                Race(
                    code="thoroughbred",
                    race_no=race_no,
                    name=race_name,
                    distance_m=pm.get("distance_m"),
                    start_time_local=pm.get("start_time_local"),
                    race_url=(meeting_url.split("#", 1)[0] + f"#Race{race_no}"),
                    extra={"class_label": parse_race_class_label(race_name)},
                )
            )
            runners_by_race[race_no] = []
        races.sort(key=lambda r: r.race_no)

    # If acceptances page had no Race1-style anchors (e.g. Caulfield Heath layout change), build races from program only
    if not races and program_meta:
        for race_no in sorted(program_meta.keys()):
            pm = program_meta[race_no]
            race_name = (pm.get("name") or "").strip() or f"Race {race_no}"
            races.append(
                Race(
                    code="thoroughbred",
                    race_no=race_no,
                    name=race_name,
                    distance_m=pm.get("distance_m"),
                    start_time_local=pm.get("start_time_local"),
                    race_url=(meeting_url.split("#", 1)[0] + f"#Race{race_no}"),
                    extra={"class_label": parse_race_class_label(race_name)},
                )
            )
            runners_by_race[race_no] = []

    if not races:
        raise ParseError("Could not find any races on acceptances page (layout may have changed).")

    # Form.aspx has jockey silks + last-start class (Acceptances does not). Best-effort enrich.
    try:
        extras = _fetch_form_extras_by_horse(key, ttl_seconds=ttl_seconds)
        runners_by_race = _apply_form_extras_to_runners(runners_by_race, extras)
    except Exception:
        pass

    # Try to infer meeting-level fields
    times = [r.start_time_local for r in races if isinstance(r.start_time_local, time)]
    first_t = min(times) if times else None

    # Update meta for conditions (used by UI)
    out_meta = {
        "track_condition": meta.get("track_condition"),
        "weather": meta.get("weather"),
        "penetrometer": meta.get("penetrometer"),
        "first_race_time_local": first_t.strftime("%H:%M") if isinstance(first_t, time) else None,
    }

    return races, runners_by_race, out_meta

