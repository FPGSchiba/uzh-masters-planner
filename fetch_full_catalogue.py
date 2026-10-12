#!/usr/bin/env python3
"""
fetch_full_catalogue.py
- Queries SemesterSet to dynamically look back from the current semester
- Crawls both Autumn (HS) and Spring (FS) sessions across all Majors and Minors
- Queries SmDetailsSet with the official browser $expand structure:
  Partof,Organizations,Responsible,Events,Events/Persons,OfferPeriods
- Resolves main lectures vs. exercise groups to prevent false self-clashes
- Resolves building and room allocations (e.g. Y15-G-60, AND-4-02)
- Correctly classifies Wahlpflicht and cross-registers electives
- Saves the complete dataset to catalogue.json
"""

import concurrent.futures
import json
import re
import ssl
import sys
import urllib.parse
import urllib.request

SSL_CTX = ssl._create_unverified_context()
BASE_URL = "https://studentservices.uzh.ch/sap/opu/odata/uzh/vvz_data_srv"
VVZ_WEB_BASE = "https://studentservices.uzh.ch/uzh/anonym/vvz/index.html#/details"
CLIENT = "001"
ROOT_SC = "SC51085510"

SM_EXPAND = "Partof,Organizations,Responsible,Events,Events/Persons,OfferPeriods"
E_EXPAND = "Schedule,Schedule/Rooms"

INF_MAJOR_IDS = ["CG51087486", "CG51087487", "CG51087484", "CG51087283", "CG51087485"]


def query_odata(path_or_url: str):
    """Executes a JSON request against the SAP OData service."""
    if path_or_url.startswith("http"):
        url = path_or_url
        if "$format=json" not in url:
            url += ("&" if "?" in url else "?") + "$format=json"
        if "sap-client" not in url:
            url += f"&sap-client={CLIENT}"
    else:
        url = f"{BASE_URL}/{path_or_url}"
        sep = "&" if "?" in url else "?"
        url += f"{sep}sap-client={CLIENT}&$format=json"

    headers = {
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    }

    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, context=SSL_CTX, timeout=14) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data.get("d", {})
    except Exception:
        return None


def parse_results(res):
    if not res:
        return []
    if isinstance(res, list):
        return res
    if isinstance(res, dict):
        if "results" in res and isinstance(res["results"], list):
            return res["results"]
        if "d" in res:
            return parse_results(res["d"])
    return []


def resolve_semesters():
    """Queries SemesterSet to identify current and previous semester sessions."""
    data = query_odata("SemesterSet")
    raw = parse_results(data)

    print(">>> 1. Discovering academic semesters via SemesterSet...", flush=True)
    for r in raw:
        print(f"    • {r.get('Text')} ({r.get('Short')}) -> PiqYear='{r.get('PiqYear')}', PiqSession='{r.get('PiqSession')}'", flush=True)

    latest_fs = next((r for r in raw if r.get("PiqSession") == "004"), None)
    latest_hs = next((r for r in raw if r.get("PiqSession") == "003"), None)

    targets = []
    if latest_hs:
        targets.append({
            "peryr": latest_hs.get("PiqYear", "2026"),
            "perid": "003",
            "term": "HS",
            "label": latest_hs.get("Text", "Herbstsemester")
        })
    else:
        targets.append({"peryr": "2026", "perid": "003", "term": "HS", "label": "Herbstsemester 2026"})

    if latest_fs:
        targets.append({
            "peryr": latest_fs.get("PiqYear", "2025"),
            "perid": "004",
            "term": "FS",
            "label": latest_fs.get("Text", "Frühjahrssemester")
        })
    else:
        targets.append({"peryr": "2025", "perid": "004", "term": "FS", "label": "Frühjahrssemester 2026"})

    return targets


def parse_schedule_string(schedule_text: str, default_room: str = ""):
    """Parses raw schedule strings (e.g., 'Di 10:15-12:00, Do 10:15-12:00') into normalized time slots."""
    if not schedule_text or "nach vereinbarung" in schedule_text.lower():
        return []

    clean = re.sub(r'<[^>]+>', ' ', schedule_text)
    day_map = {
        'mo': ('Mon', 1), 'montag': ('Mon', 1), 'mon': ('Mon', 1), 'monday': ('Mon', 1),
        'di': ('Tue', 2), 'dienstag': ('Tue', 2), 'tue': ('Tue', 2), 'tuesday': ('Tue', 2),
        'mi': ('Wed', 3), 'mittwoch': ('Wed', 3), 'wed': ('Wed', 3), 'wednesday': ('Wed', 3),
        'do': ('Thu', 4), 'donnerstag': ('Thu', 4), 'thu': ('Thu', 4), 'thursday': ('Thu', 4),
        'fr': ('Fri', 5), 'freitag': ('Fri', 5), 'fri': ('Fri', 5), 'friday': ('Fri', 5),
        'sa': ('Sat', 6), 'samstag': ('Sat', 6), 'sat': ('Sat', 6), 'saturday': ('Sat', 6),
    }

    pattern = re.compile(
        r'\b(Montag|Dienstag|Mittwoch|Donnerstag|Freitag|Samstag|Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Mo|Di|Mi|Do|Fr|Sa|Mon|Tue|Wed|Thu|Fri|Sat)\b'
        r'[:\s,]*'
        r'(?:von|from)?\s*'
        r'(\d{1,2})[:.](\d{2})\s*(?:-|bis|to)\s*(\d{1,2})[:.](\d{2})',
        re.IGNORECASE
    )

    clean_room = default_room if default_room not in ("siehe Details", "siehe Raumangabe", "(empty)", "None") else ""

    slots = []
    for match in pattern.finditer(clean):
        day_raw = match.group(1).lower()
        sh, sm, eh, em = int(match.group(2)), int(match.group(3)), int(match.group(4)), int(match.group(5))

        day_info = day_map.get(day_raw)
        if not day_info:
            continue

        day_code, day_num = day_info
        start_min = sh * 60 + sm
        end_min = eh * 60 + em

        slot = {
            "day": day_code,
            "day_num": day_num,
            "start": f"{sh:02d}:{sm:02d}",
            "end": f"{eh:02d}:{em:02d}",
            "start_min": start_min,
            "end_min": end_min,
            "room": clean_room,
            "text": f"{day_code} {sh:02d}:{sm:02d}-{eh:02d}:{em:02d}" + (f" ({clean_room})" if clean_room else "")
        }

        if not any(s["day"] == slot["day"] and s["start_min"] == slot["start_min"] and s["end_min"] == slot["end_min"] for s in slots):
            slots.append(slot)

    return slots


def is_exercise_group(title: str) -> bool:
    """Flags exercise and tutorial sections to separate them from main lectures."""
    t = title.lower()
    return any(kw in t for kw in ["übung", "uebung", "exercise", "tutorial", "gr.", "gruppe", "study center", "ausweichgruppe"])


def fetch_module_full_schedule(sm_id: str, peryr: str, perid: str):
    """
    Queries SmDetailsSet with the official browser $expand set.
    Extracts main lecture appointments and specific room numbers.
    """
    url = f"SmDetailsSet(SmObjId='{sm_id}',PiqYear='{peryr}',PiqSession='{perid}')?$expand={urllib.parse.quote(SM_EXPAND)}"
    data = query_odata(url)

    if not data:
        return {"schedule_text": "", "time_slots": [], "offer_pattern": "", "events": []}

    offer_pattern = (data.get("OfferpatternText") or "").strip()
    events_raw = parse_results(data.get("Events"))

    parsed_events = []
    for ev in events_raw:
        e_id = ev.get("Objid") or ev.get("EObjId")
        title = (ev.get("EStext") or ev.get("Stext") or "").strip()
        sched_text = (ev.get("ScheduleText") or "").strip()
        room_text = (ev.get("RoomText") or "").strip()

        # If RoomText is 'siehe Details', inspect EDetailsSet
        if room_text in ("", "siehe Details", "siehe Raumangabe") and e_id and sched_text:
            e_url = f"EDetailsSet(EObjId='{e_id}',PiqYear='{peryr}',PiqSession='{perid}')?$expand={urllib.parse.quote(E_EXPAND)}"
            e_data = query_odata(e_url)
            if e_data and isinstance(e_data, dict):
                sched_records = parse_results(e_data.get("Schedule"))
                for sc in sched_records:
                    rooms_list = parse_results(sc.get("Rooms"))
                    if rooms_list and rooms_list[0].get("Name"):
                        room_text = rooms_list[0].get("Name").strip()
                        break

        slots = parse_schedule_string(sched_text, room_text)
        is_exercise = is_exercise_group(title)

        parsed_events.append({
            "event_id": str(e_id),
            "title": title,
            "is_lecture": not is_exercise,
            "schedule_text": sched_text,
            "room": room_text if room_text not in ("siehe Details", "siehe Raumangabe", "(empty)") else "",
            "time_slots": slots
        })

    # Prioritize main lectures for weekly calendar cards
    lecture_events = [ev for ev in parsed_events if ev["is_lecture"] and ev["time_slots"]]
    chosen_events = lecture_events if lecture_events else [ev for ev in parsed_events if ev["time_slots"]]

    active_slots = []
    for ev in chosen_events:
        for s in ev["time_slots"]:
            if not any(x["day"] == s["day"] and x["start_min"] == s["start_min"] and x["end_min"] == s["end_min"] for x in active_slots):
                active_slots.append(s)

    active_slots.sort(key=lambda s: (s["day_num"], s["start_min"]))
    summary_text = ", ".join(s["text"] for s in active_slots) if active_slots else ""

    return {
        "schedule_text": summary_text,
        "time_slots": active_slots,
        "offer_pattern": offer_pattern,
        "events": parsed_events
    }


def classify_category(area_title: str, mod_title: str, is_minor: bool = False) -> str:
    """Classifies an area title into WWF Studienordnung A7.2 categories[cite: 1]."""
    if is_minor:
        return "minor"

    a = area_title.lower()
    m = mod_title.lower()

    if "masterprojekt" in a or "masterprojekt" in m or "project inf" in a or "project" in a:
        return "masterprojekt"
    if "masterarbeit" in a or "masterarbeit" in m or "thesis" in a or "thesis" in m:
        return "masterarbeit"
    # Strict order: Check 'wahlpflicht' before 'pflicht'[cite: 1]
    if "wahlpflicht" in a or "core elective" in a:
        return "wahlpflicht_major"
    if "compulsory" in a or "pflicht" in a:
        return "pflicht"
    if "wwf" in a or "wirtschaft" in a:
        return "wahl_wwf"
    if "wahlbereich" in a or "elective" in a or "inf" in a:
        return "wahl_inf"

    return "wahl_inf"


def get_child_areas(parent_otjid: str, peryr: str, perid: str):
    """Recursively resolves all leaf curriculum groups under a program[cite: 1]."""
    filter_expr = f"Peryr eq '{peryr}' and Perid eq '{perid}' and ParentOtjid eq '{parent_otjid}'"
    url = f"TreeOnDemandSet?$expand=Childs&$filter={urllib.parse.quote(filter_expr)}"
    res = query_odata(url)
    items = parse_results(res)

    areas = []
    for item in items:
        cg_id = item.get("Objid")
        title = item.get("Stext", "").strip()
        otjid = item.get("Otjid", "")
        cat = item.get("Cgcat", "")

        sub_children = parse_results(item.get("Childs"))
        if not sub_children:
            sub_filter = f"Peryr eq '{peryr}' and Perid eq '{perid}' and ParentOtjid eq '{otjid}'"
            sub_res = query_odata(f"TreeOnDemandSet?$expand=Childs&$filter={urllib.parse.quote(sub_filter)}")
            sub_children = parse_results(sub_res)

        if sub_children:
            areas.extend(get_child_areas(otjid, peryr, perid))
        else:
            areas.append({"cg_id": cg_id, "otjid": otjid, "title": title, "cat": cat})

    return areas


def fetch_modules_for_area(cg_id: str, peryr: str, perid: str):
    """Queries SmListSet with explicit PiqYear and PiqSession."""
    filter_expr = f"CgId eq '{cg_id}' and PiqYear eq '{peryr}' and PiqSession eq '{perid}'"
    url = f"SmListSet?$filter={urllib.parse.quote(filter_expr)}"
    res = query_odata(url)
    raw_modules = parse_results(res)

    if not raw_modules:
        fallback_url = f"SmListSet?$filter={urllib.parse.quote(f'CgId eq \'{cg_id}\'')}"
        raw_fallback = parse_results(query_odata(fallback_url))
        if raw_fallback:
            session_match = [m for m in raw_fallback if m.get("PiqSession") == perid]
            raw_modules = session_match if session_match else raw_fallback

    parsed = []
    for m in raw_modules:
        sm_id = m.get("SmObjId")
        name = m.get("SmText", "").strip()
        if not sm_id or not name:
            continue

        pts_raw = str(m.get("Points", "6")).strip()
        try:
            ects = int(float(pts_raw))
        except ValueError:
            ects = 6

        session = m.get("PiqSession") or perid
        year = m.get("PiqYear") or peryr

        parsed.append({
            "sm_id": sm_id,
            "name": name,
            "ects": ects,
            "year": year,
            "session": session,
            "url": f"{VVZ_WEB_BASE}/{year}/{session}/SM/{sm_id}"
        })
    return parsed


def main():
    semesters = resolve_semesters()

    primary_sem = semesters[0]
    root_filter = f"Peryr eq '{primary_sem['peryr']}' and Perid eq '{primary_sem['perid']}' and ParentOtjid eq '{ROOT_SC}'"
    root_data = query_odata(f"TreeOnDemandSet?$expand=Childs&$filter={urllib.parse.quote(root_filter)}")
    programs = parse_results(root_data)

    if not programs:
        print(f"[!] No programs found under {ROOT_SC}.", flush=True)
        sys.exit(1)

    discovered_majors = {}
    discovered_minors = {}
    program_list = []

    for p in programs:
        p_id = p.get("Otjid")
        p_name = p.get("Stext", "").strip()
        p_type = "major" if "Major" in p.get("Cgcat", "") else "minor"

        prog_entry = {"id": p_id, "name": p_name, "type": p_type}
        program_list.append(prog_entry)

        if p_type == "major":
            discovered_majors[p_id] = prog_entry
        else:
            discovered_minors[p_id] = prog_entry

    print(f"\n>>> 2. Loaded {len(discovered_majors)} Majors and {len(discovered_minors)} Minors from {ROOT_SC}.", flush=True)

    catalogue_modules = {}
    area_cache = {}

    for sem in semesters:
        peryr = sem["peryr"]
        perid = sem["perid"]
        term_code = sem["term"]

        print(f"\n--- Crawling {term_code}: {sem['label']} (PiqYear={peryr}, PiqSession={perid}) ---", flush=True)

        for prog in program_list:
            p_id = prog["id"]
            p_name = prog["name"]
            p_type = prog["type"]
            prog_clean_id = p_id.replace("CG", "").replace("SC", "")

            areas = get_child_areas(p_id, peryr, perid)
            areas.append({"cg_id": prog_clean_id, "otjid": p_id, "title": p_name, "cat": "Root"})

            for area in areas:
                cg_id = area["cg_id"]
                area_title = area["title"]

                cache_key = f"{cg_id}_{peryr}_{perid}"
                if cache_key not in area_cache:
                    area_cache[cache_key] = fetch_modules_for_area(cg_id, peryr, perid)

                mods = area_cache[cache_key]
                for m in mods:
                    sm_key = f"SM_{m['sm_id']}"
                    cat = classify_category(area_title, m["name"], is_minor=(p_type == "minor"))

                    if sm_key not in catalogue_modules:
                        catalogue_modules[sm_key] = {
                            "id": sm_key,
                            "sm_id": m["sm_id"],
                            "name": m["name"],
                            "ects": m["ects"],
                            "url": m["url"],
                            "seen_sessions": set(),
                            "programs": {}
                        }

                    catalogue_modules[sm_key]["seen_sessions"].add(perid)

                    if p_id not in catalogue_modules[sm_key]["programs"]:
                        catalogue_modules[sm_key]["programs"][p_id] = {
                            "program_id": p_id,
                            "program_name": p_name,
                            "program_type": p_type,
                            "categories": []
                        }

                    prog_entry = catalogue_modules[sm_key]["programs"][p_id]
                    existing_cats = [c["category"] for c in prog_entry["categories"]]

                    if cat not in existing_cats:
                        prog_entry["categories"].append({
                            "category": cat,
                            "group": area_title
                        })

                    # Cross-register Core Electives to general electives[cite: 1]
                    if cat == "wahlpflicht_major":
                        if "wahl_inf" not in [c["category"] for c in prog_entry["categories"]]:
                            prog_entry["categories"].append({
                                "category": "wahl_inf",
                                "group": "Wahlbereich INF (Surplus / General Elective)"
                            })
                        if "wahl_wwf" not in [c["category"] for c in prog_entry["categories"]]:
                            prog_entry["categories"].append({
                                "category": "wahl_wwf",
                                "group": "Wahlbereich WWF (Faculty Elective)"
                            })
                    elif cat == "wahl_inf":
                        if "wahl_wwf" not in [c["category"] for c in prog_entry["categories"]]:
                            prog_entry["categories"].append({
                                "category": "wahl_wwf",
                                "group": "Wahlbereich WWF (Faculty Elective)"
                            })

    # Ensure Analysis II (50809198) attached to Mathematics Minor (CG50753244)
    if "SM_50809198" not in catalogue_modules:
        catalogue_modules["SM_50809198"] = {
            "id": "SM_50809198",
            "sm_id": "50809198",
            "name": "Analysis II",
            "ects": 9,
            "url": f"{VVZ_WEB_BASE}/2025/004/SM/50809198",
            "seen_sessions": {"004"},
            "programs": {
                "CG50753244": {
                    "program_id": "CG50753244",
                    "program_name": "Mathematics",
                    "program_type": "minor",
                    "categories": [{"category": "minor", "group": "Mathematics"}]
                }
            }
        }

    # Cross-Major Elective Availability[cite: 1]
    for mod in catalogue_modules.values():
        belongs_to_any_inf = any(p_id in mod["programs"] for p_id in INF_MAJOR_IDS)
        if belongs_to_any_inf:
            for inf_id in INF_MAJOR_IDS:
                if inf_id not in mod["programs"]:
                    mod["programs"][inf_id] = {
                        "program_id": inf_id,
                        "program_name": discovered_majors.get(inf_id, {}).get("name", "Informatics"),
                        "program_type": "major",
                        "categories": [
                            {"category": "wahl_inf", "group": "Wahlbereich INF (Cross-Program Elective)"},
                            {"category": "wahl_wwf", "group": "Wahlbereich WWF"}
                        ]
                    }

    print(f"\n>>> 3. Concurrently retrieving event schedules (SM -> Events) for {len(catalogue_modules)} modules...", flush=True)

    details_map = {}

    def fetch_worker(m_tuple):
        sm_id, seen_sess = m_tuple
        if "003" in seen_sess:
            res = fetch_module_full_schedule(sm_id, "2026", "003")
            if res["time_slots"]:
                return sm_id, res
        if "004" in seen_sess:
            res = fetch_module_full_schedule(sm_id, "2025", "004")
            if res["time_slots"]:
                return sm_id, res
        return sm_id, fetch_module_full_schedule(sm_id, "2026", "003")

    worker_items = [(m["sm_id"], m["seen_sessions"]) for m in catalogue_modules.values()]

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        future_to_id = {executor.submit(fetch_worker, item): item[0] for item in worker_items}
        completed = 0
        for future in concurrent.futures.as_completed(future_to_id):
            sm_id, result = future.result()
            details_map[sm_id] = result
            completed += 1
            if completed % 35 == 0 or completed == len(worker_items):
                print(f"   Progress: {completed}/{len(worker_items)} modules resolved...", flush=True)

    count_hs = 0
    count_fs = 0
    count_both = 0
    scheduled_count = 0

    for mod in catalogue_modules.values():
        sm_id = mod["sm_id"]
        det = details_map.get(sm_id, {"schedule_text": "", "time_slots": [], "offer_pattern": "", "events": []})
        mod["schedule_text"] = det["schedule_text"]
        mod["time_slots"] = det["time_slots"]
        mod["events"] = det.get("events", [])
        mod["offer_pattern"] = det["offer_pattern"]

        if mod["time_slots"]:
            scheduled_count += 1

        seen = mod.pop("seen_sessions", set())
        if "003" in seen and "004" in seen:
            term = "both"
        elif "004" in seen:
            term = "FS"
        elif "003" in seen:
            term = "HS"
        else:
            term = "both"

        op = det["offer_pattern"].lower()
        if "herbst" in op and "frühjahr" not in op:
            term = "HS"
        elif "frühjahr" in op and "herbst" not in op:
            term = "FS"
        elif "jedes semester" in op or ("herbst" in op and "frühjahr" in op):
            term = "both"

        mod["term"] = term
        if term == "HS":
            count_hs += 1
        elif term == "FS":
            count_fs += 1
        else:
            count_both += 1

    wp_ai_count = sum(
        1 for m in catalogue_modules.values()
        if "CG51087486" in m["programs"] and any(c["category"] == "wahlpflicht_major" for c in m["programs"]["CG51087486"]["categories"])
    )

    print(f"\n>>> Crawl Verification Summary:")
    print(f"    • Total Unique Modules:             {len(catalogue_modules)}")
    print(f"    • Modules with Live Lecture Hours:  {scheduled_count}")
    print(f"    • Pure HS (Autumn):                 {count_hs}")
    print(f"    • Pure FS (Spring):                 {count_fs}")
    print(f"    • Both (HS/FS):                     {count_both}")
    print(f"    • AI Major Wahlpflicht Modules:     {wp_ai_count}")

    final_catalogue = {
        "metadata": {
            "root": ROOT_SC,
            "crawled_terms": [s["label"] for s in semesters],
            "total_modules": len(catalogue_modules)
        },
        "majors": list(discovered_majors.values()),
        "minors": list(discovered_minors.values()),
        "modules": list(catalogue_modules.values())
    }

    with open("catalogue.json", "w", encoding="utf-8") as f:
        json.dump(final_catalogue, f, indent=2, ensure_ascii=False)

    print("✓ Successfully saved catalogue.json with live lecture schedules and room allocations!")


if __name__ == "__main__":
    main()