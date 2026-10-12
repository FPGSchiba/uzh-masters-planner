#!/usr/bin/env python3
"""
fetch_full_catalogue.py - Best-of Merged Crawler
- Queries SemesterSet to dynamically identify relevant HS and FS sessions
- Crawls Autumn (HS) and Spring (FS) sessions across all Majors and Minors
- Queries SmDetailsSet with official SAP browser headers & expand parameters
- Deeply resolves rooms from EDetailsSet when RoomText is 'siehe Details'
- Maps room codes to official UZH Campuses (Oerlikon, Zentrum, Irchel, Schlieren, Online)
- Differentiates main lectures from exercise groups to avoid false duplicate clashing
- Correctly classifies Wahlpflicht (checked before Pflicht)
- Cross-registers Core Electives into Wahlbereich INF and Wahlbereich WWF
- Makes modules from other Informatics majors available as electives
- Saves output into catalogue.json
"""

import concurrent.futures
import json
import re
import ssl
import sys
import urllib.parse
import urllib.request

SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE

BASE_URL = "https://studentservices.uzh.ch/sap/opu/odata/uzh/vvz_data_srv"
VVZ_WEB_BASE = "https://studentservices.uzh.ch/uzh/anonym/vvz/index.html#/details"
CLIENT = "001"
ROOT_SC = "SC51085510"

SM_EXPAND = "Partof,Organizations,Responsible,Events,Events/Persons,OfferPeriods"
E_EXPAND = "Rooms,Persons,Schedule,Schedule/Rooms,Schedule/Persons,Modules,Links"

INF_MAJOR_IDS = ["CG51087486", "CG51087487", "CG51087484", "CG51087283", "CG51087485"]

DEFAULT_HEADERS = {
    "Accept": "application/json",
    "Accept-Language": "de-CH,de;q=0.9,en;q=0.8",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "X-Requested-With": "XMLHttpRequest",
    "DataServiceVersion": "2.0",
    "MaxDataServiceVersion": "2.0",
}

CAMPUS_MAP = {
    "BIN": "Oerlikon",
    "AND": "Oerlikon",
    "AFL": "Oerlikon",
    "WAG": "Schlieren",
    "WAD": "Schlieren",
}


def map_room_to_campus(room_str: str) -> str:
    """Maps a room string (e.g. 'BIN-1-B.01', 'KOL-F-118', 'Y17-M-05') to its UZH campus."""
    if not room_str or not isinstance(room_str, str):
        return ""
    r = room_str.strip()
    low = r.lower()

    if any(k in low for k in ["online", "zoom", "teams", "virtuell", "podcast", "fernunterricht"]):
        return "Online"
    if low in ["siehe details", "s.hinweise", "s. hinweise", "nach vereinbarung", "n.v.", "n. v.", ""]:
        return ""

    m = re.match(r"^([A-Za-z0-9]+)", r)
    if not m:
        return ""
    bldg = m.group(1).upper()

    if bldg in CAMPUS_MAP:
        return CAMPUS_MAP[bldg]

    if bldg.startswith("Y") and len(bldg) >= 2 and (bldg[1].isdigit() or bldg == "Y"):
        return "Irchel"
    if bldg in ["TFA", "TFB"]:
        return "Irchel"

    return "Zentrum"


def is_exercise_group(title: str) -> bool:
    """Flags exercise and tutorial sections to separate them from main lectures."""
    t = title.lower()
    return any(kw in t for kw in ["übung", "uebung", "exercise", "tutorial", "gr.", "gruppe", "study center", "ausweichgruppe"])


def query_odata(path_or_url: str):
    """Executes a JSON request against the SAP OData service with official headers."""
    if path_or_url.startswith("http"):
        url = path_or_url
        if "$format=json" not in url:
            sep = "&" if "?" in url else "?"
            url += f"{sep}$format=json"
        if "sap-client" not in url:
            url += f"&sap-client={CLIENT}"
    else:
        url = f"{BASE_URL}/{path_or_url}"
        sep = "&" if "?" in url else "?"
        url += f"{sep}sap-client={CLIENT}&$format=json"

    req = urllib.request.Request(url, headers=DEFAULT_HEADERS)
    try:
        with urllib.request.urlopen(req, context=SSL_CTX, timeout=14) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data.get("d", {})
    except Exception:
        return None


def parse_results(container):
    """Safely extracts results from OData structure."""
    if not container:
        return []
    if isinstance(container, list):
        return container
    if isinstance(container, dict):
        if "results" in container and isinstance(container["results"], list):
            return container["results"]
        if "d" in container:
            return parse_results(container["d"])
        return [container]
    return []


def resolve_semesters():
    """Queries SemesterSet to identify current and populated semester sessions."""
    data = query_odata("SemesterSet")
    raw = parse_results(data)

    print(">>> 1. Discovering academic semesters via SemesterSet...", flush=True)
    for r in raw:
        print(f"    • {r.get('Text')} ({r.get('Short')}) -> PiqYear='{r.get('PiqYear')}', PiqSession='{r.get('PiqSession')}'", flush=True)

    # Pick the most relevant populated semesters
    targets = [
        {"peryr": "2025", "perid": "003", "term": "HS", "label": "Herbstsemester 2025"},
        {"peryr": "2025", "perid": "004", "term": "FS", "label": "Frühjahrssemester 2026"}
    ]

    has_hs26 = any(r.get("PiqYear") == "2026" and r.get("PiqSession") == "003" for r in raw)
    if has_hs26:
        targets.append({"peryr": "2026", "perid": "003", "term": "HS", "label": "Herbstsemester 2026"})

    return targets


def parse_schedule_string(schedule_text: str, default_room: str = ""):
    """Parses raw German and English schedule strings into normalized time slots with room tagging."""
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

    clean_room = default_room.strip() if default_room and default_room not in ("siehe Details", "siehe Raumangabe", "s.Hinweise", "(empty)", "None") else ""

    pattern = re.compile(
        r'\b(Mo(?:ntag)?|Di(?:enstag)?|Mi(?:ttwoch)?|Do(?:nnerstag)?|Fr(?:eitag)?|Sa(?:mstag)?|Mon(?:day)?|Tue(?:sday)?|Wed(?:nesday)?|Thu(?:rsday)?|Fri(?:day)?)\b'
        r'[^\d]*?'
        r'(\d{1,2})[:.](\d{2})'
        r'\s*(?:-|bis|to)\s*'
        r'(\d{1,2})[:.](\d{2})',
        re.IGNORECASE
    )

    slots = []
    for match in pattern.finditer(clean):
        day_raw = match.group(1).lower()
        if day_raw not in day_map:
            continue
        day_code, day_num = day_map[day_raw]

        sh, sm = int(match.group(2)), int(match.group(3))
        eh, em = int(match.group(4)), int(match.group(5))

        start_min = sh * 60 + sm
        end_min = eh * 60 + em

        if end_min <= start_min:
            continue

        slot_text = f"{day_code} {sh:02d}:{sm:02d}-{eh:02d}:{em:02d}"
        if clean_room:
            slot_text += f" ({clean_room})"

        slot = {
            "day": day_code,
            "day_num": day_num,
            "start": f"{sh:02d}:{sm:02d}",
            "end": f"{eh:02d}:{em:02d}",
            "start_min": start_min,
            "end_min": end_min,
            "room": clean_room,
            "text": slot_text
        }

        if not any(s["day"] == slot["day"] and s["start_min"] == slot["start_min"] and s["end_min"] == slot["end_min"] for s in slots):
            slots.append(slot)

    return slots


def fetch_module_full_schedule(sm_id: str, candidate_sessions: list):
    """
    Fetches full module schedule, events, and rooms from SmDetailsSet across candidate sessions.
    Differentiates lectures from exercises and resolves rooms via EDetailsSet.
    """
    best_events_raw = []
    best_offer_pattern = ""
    best_sched_desc = ""
    chosen_peryr = ""
    chosen_perid = ""

    for y, s in candidate_sessions:
        url = f"SmDetailsSet(SmObjId='{sm_id}',PiqYear='{y}',PiqSession='{s}')?$expand={urllib.parse.quote(SM_EXPAND)}"
        res = query_odata(url)
        if res:
            events = parse_results(res.get("Events"))
            op = (res.get("OfferpatternText") or "").strip()
            ad = (res.get("AppointmentsDescription") or "").strip()
            if op and not best_offer_pattern:
                best_offer_pattern = op
            if ad and len(ad) > len(best_sched_desc):
                best_sched_desc = ad
            if events and not best_events_raw:
                best_events_raw = events
                chosen_peryr = y
                chosen_perid = s

    rooms_set = set()
    parsed_events = []

    for ev in best_events_raw:
        e_id = ev.get("Objid") or ev.get("EObjId")
        title = (ev.get("EStext") or ev.get("Stext") or "").strip()
        sched_text = (ev.get("ScheduleText") or "").strip()
        room_text = (ev.get("RoomText") or "").strip()

        # If RoomText is 'siehe Details', inspect EDetailsSet
        if room_text in ("", "siehe Details", "siehe Raumangabe", "s.Hinweise") and e_id:
            e_url = f"EDetailsSet(EObjId='{e_id}',PiqYear='{chosen_peryr}',PiqSession='{chosen_perid}')?$expand={urllib.parse.quote(E_EXPAND)}"
            e_data = query_odata(e_url)
            if e_data and isinstance(e_data, dict):
                # Check direct Rooms list
                rooms_list = parse_results(e_data.get("Rooms"))
                if rooms_list and rooms_list[0].get("Name"):
                    room_text = rooms_list[0].get("Name").strip()
                    for r in rooms_list:
                        rn = (r.get("Name") or "").strip()
                        if rn:
                            rooms_set.add(rn)
                else:
                    sched_records = parse_results(e_data.get("Schedule"))
                    for sc in sched_records:
                        sc_rooms = parse_results(sc.get("Rooms"))
                        if sc_rooms and sc_rooms[0].get("Name"):
                            room_text = sc_rooms[0].get("Name").strip()
                            for sr in sc_rooms:
                                srn = (sr.get("Name") or "").strip()
                                if srn:
                                    rooms_set.add(srn)
                            break

        if room_text and room_text not in ("", "siehe Details", "siehe Raumangabe", "s.Hinweise", "(empty)", "None"):
            rooms_set.add(room_text)

        slots = parse_schedule_string(sched_text, room_text)
        is_exercise = is_exercise_group(title)

        parsed_events.append({
            "event_id": str(e_id) if e_id else "",
            "title": title,
            "is_lecture": not is_exercise,
            "schedule_text": sched_text,
            "room": room_text if room_text not in ("siehe Details", "siehe Raumangabe", "s.Hinweise", "(empty)") else "",
            "time_slots": slots
        })

    # Prioritize main lectures for weekly timetable and card time slots
    lecture_events = [ev for ev in parsed_events if ev["is_lecture"] and ev["time_slots"]]
    chosen_events = lecture_events if lecture_events else [ev for ev in parsed_events if ev["time_slots"]]

    active_slots = []
    for ev in chosen_events:
        for s in ev["time_slots"]:
            if not any(x["day"] == s["day"] and x["start_min"] == s["start_min"] and x["end_min"] == s["end_min"] for x in active_slots):
                active_slots.append(s)

    active_slots.sort(key=lambda s: (s["day_num"], s["start_min"]))

    all_schedules = [ev["schedule_text"] for ev in parsed_events if ev["schedule_text"] and ev["schedule_text"] not in ["siehe Details", "s.Hinweise"]]
    summary_text = "; ".join(all_schedules) if all_schedules else (re.sub(r'<[^>]+>', ' ', best_sched_desc).strip() if best_sched_desc else "")

    campuses = sorted(list(set(map_room_to_campus(r) for r in rooms_set if map_room_to_campus(r))))
    campus = ", ".join(campuses) if campuses else ""

    return {
        "rooms": sorted(list(rooms_set)),
        "campuses": campuses,
        "campus": campus,
        "schedule_text": summary_text,
        "time_slots": active_slots,
        "offer_pattern": best_offer_pattern,
        "events": parsed_events
    }


def classify_category(area_title: str, mod_title: str, is_minor: bool = False) -> str:
    """Classifies an area title into WWF Studienordnung A7.2 categories."""
    if is_minor:
        return "minor"

    a = area_title.lower()
    m = mod_title.lower()

    if "masterprojekt" in a or "masterprojekt" in m or "project inf" in a or "project" in a:
        return "masterprojekt"
    if "masterarbeit" in a or "masterarbeit" in m or "thesis" in a or "thesis" in m:
        return "masterarbeit"

    # Strictly check wahlpflicht before pflicht
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
    """Recursively resolves all leaf curriculum groups under a program."""
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
    clean_cg = str(cg_id).replace("CG", "").replace("SC", "")
    filter_expr = f"CgId eq '{clean_cg}' and PiqYear eq '{peryr}' and PiqSession eq '{perid}'"
    url = f"SmListSet?$filter={urllib.parse.quote(filter_expr)}"
    res = query_odata(url)
    raw_modules = parse_results(res)

    if not raw_modules:
        fallback_filter = f"CgId eq '{clean_cg}'"
        fallback_url = f"SmListSet?$filter={urllib.parse.quote(fallback_filter)}"
        raw_fallback = parse_results(query_odata(fallback_url))
        if raw_fallback:
            session_match = [m for m in raw_fallback if m.get("PiqSession") == perid]
            raw_modules = session_match if session_match else raw_fallback

    parsed = []
    for m in raw_modules:
        sm_id = m.get("SmObjId")
        name = (m.get("SmText") or "").strip()
        if not sm_id or not name:
            continue

        pts_raw = str(m.get("Points") or m.get("Cp") or "6").strip()
        try:
            ects = int(float(pts_raw))
        except (ValueError, TypeError):
            ects = 6

        detail_url = f"{VVZ_WEB_BASE}/{peryr}/{perid}/SM/{sm_id}"

        parsed.append({
            "sm_id": str(sm_id),
            "name": name,
            "ects": ects,
            "url": detail_url
        })
    return parsed


def main():
    semesters = resolve_semesters()

    discovered_majors = {}
    discovered_minors = {}

    # Discover all majors and minors across the candidate semesters
    for sem in semesters:
        peryr = sem["peryr"]
        perid = sem["perid"]
        filter_expr = f"Peryr eq '{peryr}' and Perid eq '{perid}' and ParentOtjid eq '{ROOT_SC}'"
        root_data = query_odata(f"TreeOnDemandSet?$expand=Childs&$filter={urllib.parse.quote(filter_expr)}")
        for p in parse_results(root_data):
            p_id = p.get("Otjid")
            stext = (p.get("Stext") or "").strip()
            cgcat = (p.get("Cgcat") or "").strip()
            display_name = f"{stext} {cgcat}".strip() if cgcat else stext
            if "Major" in cgcat or "Major" in stext:
                if p_id not in discovered_majors:
                    discovered_majors[p_id] = {"id": p_id, "name": display_name}
            else:
                if p_id not in discovered_minors:
                    discovered_minors[p_id] = {"id": p_id, "name": display_name}

    majors = list(discovered_majors.values())
    minors = list(discovered_minors.values())

    print(f"\n>>> 2. Loaded {len(majors)} Majors and {len(minors)} Minors from {ROOT_SC}.", flush=True)

    catalogue_modules = {}
    area_cache = {}

    for sem in semesters:
        peryr = sem["peryr"]
        perid = sem["perid"]
        term_label = sem["term"]
        label = sem["label"]
        print(f"\n--- Crawling {term_label}: {label} (PiqYear={peryr}, PiqSession={perid}) ---", flush=True)

        for prog in majors + minors:
            p_id = prog["id"]
            p_name = prog["name"]
            is_minor = prog in minors
            prog_clean_id = p_id.replace("CG", "").replace("SC", "")

            areas = get_child_areas(p_id, peryr, perid)
            areas.append({"cg_id": prog_clean_id, "otjid": p_id, "title": p_name, "cat": "Root"})

            for area in areas:
                area_cg = area["cg_id"]
                area_title = area["title"]
                area_otjid = area.get("otjid", area_cg)

                cache_key = f"{area_cg}_{peryr}_{perid}"
                if cache_key not in area_cache:
                    area_cache[cache_key] = fetch_modules_for_area(area_cg, peryr, perid)

                mods = area_cache[cache_key]
                for m in mods:
                    sm_id = m["sm_id"]
                    sm_key = f"SM_{sm_id}"
                    cat = classify_category(area_title, m["name"], is_minor=is_minor)

                    if sm_key not in catalogue_modules:
                        catalogue_modules[sm_key] = {
                            "id": sm_key,
                            "sm_id": sm_id,
                            "name": m["name"],
                            "ects": m["ects"],
                            "url": m["url"],
                            "seen_sessions": set(),
                            "seen_pairs": set(),
                            "programs": {}
                        }

                    catalogue_modules[sm_key]["seen_sessions"].add(perid)
                    catalogue_modules[sm_key]["seen_pairs"].add((peryr, perid))

                    if p_id not in catalogue_modules[sm_key]["programs"]:
                        catalogue_modules[sm_key]["programs"][p_id] = {
                            "program_id": p_id,
                            "program_name": p_name,
                            "is_minor": is_minor,
                            "categories": []
                        }

                    prog_entry = catalogue_modules[sm_key]["programs"][p_id]
                    if not any(c["category"] == cat for c in prog_entry["categories"]):
                        prog_entry["categories"].append({
                            "category": cat,
                            "group_id": area_otjid,
                            "group": area_title
                        })

                    # Cross-register Core Electives to general electives
                    if cat == "wahlpflicht_major":
                        if "wahl_inf" not in [c["category"] for c in prog_entry["categories"]]:
                            prog_entry["categories"].append({
                                "category": "wahl_inf",
                                "group_id": area_otjid,
                                "group": "Wahlbereich INF (Elective)"
                            })
                        if "wahl_wwf" not in [c["category"] for c in prog_entry["categories"]]:
                            prog_entry["categories"].append({
                                "category": "wahl_wwf",
                                "group_id": area_otjid,
                                "group": "Wahlbereich WWF (Faculty Elective)"
                            })
                    elif cat == "wahl_inf":
                        if "wahl_wwf" not in [c["category"] for c in prog_entry["categories"]]:
                            prog_entry["categories"].append({
                                "category": "wahl_wwf",
                                "group_id": area_otjid,
                                "group": "Wahlbereich WWF (Faculty Elective)"
                            })

    # Direct Failsafe: Ensure Analysis II (50809198) attached to Mathematics Minor (CG50753244)
    if "SM_50809198" not in catalogue_modules:
        catalogue_modules["SM_50809198"] = {
            "id": "SM_50809198",
            "sm_id": "50809198",
            "name": "Analysis II",
            "ects": 9,
            "url": f"{VVZ_WEB_BASE}/2025/004/SM/50809198",
            "seen_sessions": {"004"},
            "seen_pairs": {("2025", "004")},
            "programs": {
                "CG50753244": {
                    "program_id": "CG50753244",
                    "program_name": "Mathematics Minor 30",
                    "is_minor": True,
                    "categories": [{"category": "minor", "group_id": "manual", "group": "Analysis II"}]
                }
            }
        }

    # Cross-Major Elective Availability
    for mod in catalogue_modules.values():
        belongs_to_any_inf = any(p_id in mod["programs"] for p_id in INF_MAJOR_IDS)
        if belongs_to_any_inf:
            for inf_id in INF_MAJOR_IDS:
                if inf_id not in mod["programs"]:
                    mod["programs"][inf_id] = {
                        "program_id": inf_id,
                        "program_name": discovered_majors.get(inf_id, {}).get("name", "Informatics Cross-Major Elective"),
                        "is_minor": False,
                        "categories": [
                            {"category": "wahl_inf", "group_id": "cross_inf", "group": "Wahlbereich INF"},
                            {"category": "wahl_wwf", "group_id": "cross_wwf", "group": "Wahlbereich WWF"}
                        ]
                    }

    print(f"\n>>> 3. Concurrently fetching schedule details, rooms, and campuses for {len(catalogue_modules)} modules...", flush=True)

    details_map = {}

    def fetch_worker(item):
        sm_id, seen_pairs = item
        candidates = list(seen_pairs)
        for fallback in [("2025", "003"), ("2025", "004"), ("2026", "003"), ("2024", "004")]:
            if fallback not in candidates:
                candidates.append(fallback)
        return sm_id, fetch_module_full_schedule(sm_id, candidates)

    work_items = [(m["sm_id"], list(m.get("seen_pairs", []))) for m in catalogue_modules.values()]

    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as executor:
        completed = 0
        future_to_id = {executor.submit(fetch_worker, item): item[0] for item in work_items}
        for future in concurrent.futures.as_completed(future_to_id):
            sm_id = future_to_id[future]
            try:
                _, res = future.result()
                details_map[sm_id] = res
            except Exception:
                details_map[sm_id] = {
                    "rooms": [],
                    "campuses": [],
                    "campus": "",
                    "schedule_text": "",
                    "time_slots": [],
                    "events": [],
                    "offer_pattern": ""
                }
            completed += 1
            if completed % 50 == 0 or completed == len(work_items):
                print(f"   Progress: {completed}/{len(work_items)} schedules fetched...", flush=True)

    count_hs = 0
    count_fs = 0
    count_both = 0
    campus_counts = {}
    scheduled_count = 0

    for mod in catalogue_modules.values():
        sm_id = mod["sm_id"]
        det = details_map.get(sm_id, {
            "rooms": [],
            "campuses": [],
            "campus": "",
            "schedule_text": "",
            "time_slots": [],
            "events": [],
            "offer_pattern": ""
        })

        mod["rooms"] = det["rooms"]
        mod["campuses"] = det["campuses"]
        mod["campus"] = det["campus"]
        mod["schedule_text"] = det["schedule_text"]
        mod["time_slots"] = det["time_slots"]
        mod["events"] = det.get("events", [])
        mod["offer_pattern"] = det["offer_pattern"]

        if mod["time_slots"]:
            scheduled_count += 1

        for camp in det["campuses"]:
            campus_counts[camp] = campus_counts.get(camp, 0) + 1

        seen = mod.pop("seen_sessions", set())
        mod.pop("seen_pairs", None)

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

    modules_with_slots = sum(1 for m in catalogue_modules.values() if m.get("time_slots"))
    modules_with_rooms = sum(1 for m in catalogue_modules.values() if m.get("rooms"))

    print("\n>>> Crawl Verification Summary:")
    print(f"    • Total Unique Modules:           {len(catalogue_modules)}")
    print(f"    • Modules with Time Slots:        {modules_with_slots}")
    print(f"    • Modules with Rooms/Campus:      {modules_with_rooms}")
    print(f"    • Pure HS (Autumn):               {count_hs}")
    print(f"    • Pure FS (Spring):               {count_fs}")
    print(f"    • Both (HS/FS):                   {count_both}")
    print(f"    • AI Major Wahlpflicht Modules:   {wp_ai_count}")
    print(f"    • Campus Distribution:            {campus_counts}")

    final_catalogue = {
        "metadata": {
            "root": ROOT_SC,
            "majors_count": len(majors),
            "minors_count": len(minors),
            "total_modules": len(catalogue_modules),
            "crawled_semesters": [s["label"] for s in semesters]
        },
        "majors": majors,
        "minors": minors,
        "modules": list(catalogue_modules.values())
    }

    with open("catalogue.json", "w", encoding="utf-8") as f:
        json.dump(final_catalogue, f, indent=2, ensure_ascii=False)

    print("✓ Successfully saved catalogue.json with full campuses, rooms, and schedules!")


if __name__ == "__main__":
    main()
