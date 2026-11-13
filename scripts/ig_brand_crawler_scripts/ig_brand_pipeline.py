"""Read-only mention discovery and resumable Instagram profile collection.

Run from ig_brand_crawler.ipynb. Source CSVs are never opened for writing.
"""
from __future__ import annotations

import csv
import json
import random
import re
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

USERNAME = re.compile(r"[a-z0-9_](?:[a-z0-9_.]{0,28}[a-z0-9_])?", re.I)
# A preceding letter/digit excludes email addresses; consume the entire token
# before validating, so malformed / >30-character handles are not truncated.
MENTION = re.compile(r"(?<![a-zA-Z0-9_@.+-])[@＠]([a-zA-Z0-9_.]+)")
BRAND_FIELDS = ["brand_id", "brand_name", "number_of_followers",
                "number_of_followees", "number_of_published_posts",
                "business_type_of_the_brand"]


def normalize_username(value):
    value = (value or "").strip().lstrip("@＠").lower()
    return value if USERNAME.fullmatch(value) and ".." not in value else ""


def extract_mentions(text):
    result = []
    for token in MENTION.findall(text or ""):
        # A trailing sentence period is not part of an Instagram username.
        username = normalize_username(token.rstrip("."))
        if username:
            result.append(username)
    return result


def safe_output(source_dir, output_dir):
    source, output = Path(source_dir).resolve(), Path(output_dir).resolve()
    if output == source or source in output.parents or output in source.parents:
        raise ValueError("輸出資料夾必須與來源資料夾分離，禁止寫入資料庫。")
    output.mkdir(parents=True, exist_ok=True)
    return output


def write_csv(path, fields, rows):
    path = Path(path)
    if path.is_symlink() or path.with_suffix(path.suffix + ".tmp").is_symlink():
        raise ValueError(f"拒絕寫入符號連結：{path}")
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)


def read_csv(path):
    with Path(path).open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def discover_candidates(source_dir, output_dir):
    """Two passes ensure even mentions of owners in later files are excluded."""
    source = Path(source_dir).resolve()
    output = safe_output(source, output_dir)
    files = sorted(source.glob("*-FullVideoInfo.csv"))
    if not files:
        raise FileNotFoundError(f"找不到 *-FullVideoInfo.csv：{source}")
    influencers = set()
    for file in files:
        influencers.add(normalize_username(file.name.removesuffix("-FullVideoInfo.csv")))
        with file.open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            required = {"text", "post_owner.username"}
            if not required.issubset(reader.fieldnames or []):
                raise ValueError(f"{file.name} 缺少欄位：{required - set(reader.fieldnames or [])}")
            influencers.update(normalize_username(row["post_owner.username"]) for row in reader)
    influencers.discard("")
    counts = Counter()
    post_keys, owners = defaultdict(set), defaultdict(set)
    evidence = []
    total_rows = excluded = 0
    for file in files:
        with file.open(encoding="utf-8-sig", newline="") as handle:
            for row_number, row in enumerate(csv.DictReader(handle), start=2):
                total_rows += 1
                owner = normalize_username(row["post_owner.username"])
                mentions = Counter(extract_mentions(row["text"]))
                for username, count in mentions.items():
                    if username in influencers:
                        excluded += count
                        continue
                    counts[username] += count
                    # Same post duplicated across files is counted once here.
                    key = row.get("id") or row.get("short_code") or f"{file.name}:{row_number}"
                    post_keys[username].add(key)
                    if owner:
                        owners[username].add(owner)
                    evidence.append(dict(brand_id=username, source_file=file.name,
                                         source_row=row_number, influencer_username=owner,
                                         post_id=row.get("id", ""), short_code=row.get("short_code", ""),
                                         occurrences=count, text=row["text"]))
    candidates = [dict(brand_id=u, mention_count=counts[u],
                       post_count=len(post_keys[u]), influencer_count=len(owners[u]),
                       profile_url=f"https://www.instagram.com/{u}/")
                  for u in sorted(counts, key=lambda u: (-len(owners[u]), -counts[u], u))]
    write_csv(output / "influencer_usernames.csv", ["username"],
              ({"username": u} for u in sorted(influencers)))
    write_csv(output / "brand_candidates.csv",
              ["brand_id", "mention_count", "post_count", "influencer_count", "profile_url"], candidates)
    write_csv(output / "mention_evidence.csv",
              ["brand_id", "source_file", "source_row", "influencer_username", "post_id",
               "short_code", "occurrences", "text"], evidence)
    # Preserve prior manual decisions; append newly discovered handles only.
    review_path = output / "brand_review.csv"
    reviews = read_csv(review_path) if review_path.exists() else []
    known = {r["brand_id"] for r in reviews}
    reviews.extend(dict(brand_id=r["brand_id"], decision="pending", notes="")
                   for r in candidates if r["brand_id"] not in known)
    write_csv(review_path, ["brand_id", "decision", "notes"], reviews)
    return dict(files=len(files), posts=total_rows, influencers=len(influencers),
                candidates=len(candidates), excluded_mentions=excluded)


def make_loader(login_username=None, session_file=None):
    import instaloader
    loader = instaloader.Instaloader(
        download_pictures=False, download_videos=False, download_video_thumbnails=False,
        download_geotags=False, download_comments=False, save_metadata=False,
        max_connection_attempts=1, request_timeout=30,
        fatal_status_codes=[401, 403, 429])
    if login_username:
        # Load only a trusted session created by Instaloader. Never save passwords.
        loader.load_session_from_file(login_username, filename=str(session_file) if session_file else None)
    return loader


def fetch_profile(loader, username):
    import instaloader
    profile = instaloader.Profile.from_username(loader.context, username)
    record = dict(brand_id=username, instagram_user_id=str(profile.userid),
                  resolved_username=profile.username)
    missing = {}
    for field, attr in {
        "brand_name": "full_name", "number_of_followers": "followers",
        "number_of_followees": "followees", "number_of_published_posts": "mediacount",
        "business_type_of_the_brand": "business_category_name",
        "is_business_account": "is_business_account", "biography": "biography",
        "external_url": "external_url", "is_private": "is_private",
    }.items():
        try:
            value = getattr(profile, attr)
            record[field] = value
            if value is None or value == "":
                missing[field] = "not_provided"
        except (instaloader.LoginRequiredException, KeyError) as exc:
            record[field] = None
            missing[field] = f"{type(exc).__name__}: {exc}"
    record["missing_fields"] = missing
    record["status"] = "partial" if any(f in missing for f in BRAND_FIELDS[1:]) else "ok"
    return record


def crawl_profiles(usernames, loader, source_dir, output_dir, *, max_profiles=5,
                   min_delay=15, max_delay=30, retry_failed=False, fetcher=None):
    """Append after each request; stop the batch on auth, rate, or network errors.

    Completed and partial profiles are skipped on resume. retry_failed=True
    retries errors and partial records, and retains older good data on failure.
    """
    if min_delay < 0 or max_delay < min_delay or max_profiles < 1:
        raise ValueError("請檢查 delay 與 max_profiles 設定。")
    output = safe_output(source_dir, output_dir)
    path = output / "profile_attempts.jsonl"
    if path.is_symlink():
        raise ValueError("拒絕寫入符號連結。")
    attempts = load_attempts(path)
    latest = {r["brand_id"]: r for r in attempts}
    fetcher = fetcher or fetch_profile
    results = []
    seen = set()
    for value in usernames:
        username = normalize_username(value)
        if not username or username in seen:
            continue
        seen.add(username)
        previous = latest.get(username)
        if previous and (not retry_failed or previous["status"] == "ok"):
            continue
        if len(results) >= max_profiles:
            break
        if results:
            time.sleep(random.uniform(min_delay, max_delay))
        stop = False
        try:
            record = fetcher(loader, username)
        except Exception as exc:
            name = type(exc).__name__
            record = dict(brand_id=username, status="error", error_type=name, error=str(exc))
            # Only an explicitly missing profile is safe to continue past.
            stop = name not in {"ProfileNotExistsException", "ProfileHasNoPicsException"}
        record["fetched_at_utc"] = datetime.now(timezone.utc).isoformat()
        # Never store session credentials or cookies in results.
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        results.append(record)
        print(f"{username}: {record['status']}", flush=True)
        if stop:
            print("本批次停止，請檢查 profile_attempts.jsonl 的錯誤後再續跑。", flush=True)
            break
    export_profiles(source_dir, output_dir)
    return results


def load_attempts(path):
    if not Path(path).exists():
        return []
    # Fail loudly on damaged checkpoints, rather than silently re-requesting all profiles.
    with Path(path).open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def export_profiles(source_dir, output_dir):
    output = safe_output(source_dir, output_dir)
    latest_good = {}
    for row in load_attempts(output / "profile_attempts.jsonl"):
        if row["status"] in {"ok", "partial"}:
            latest_good[row["brand_id"]] = row
    fields = BRAND_FIELDS + ["instagram_user_id", "resolved_username", "is_business_account",
                             "biography", "external_url", "is_private", "status", "fetched_at_utc", "missing_fields"]
    rows = [dict(r, missing_fields=json.dumps(r.get("missing_fields", {}), ensure_ascii=False))
            for r in latest_good.values()]
    write_csv(output / "candidate_profiles.csv", fields, rows)
    review = output / "brand_review.csv"
    decisions = read_csv(review) if review.exists() else []
    invalid = [r for r in decisions if r["decision"] not in {"pending", "brand", "not_brand", "unsure"}]
    ids = [r["brand_id"] for r in decisions]
    if invalid or len(ids) != len(set(ids)):
        raise ValueError("brand_review.csv 有無效 decision 或重複 brand_id。")
    confirmed = {r["brand_id"] for r in decisions if r["decision"] == "brand"}
    write_csv(output / "brands.csv", BRAND_FIELDS,
              (r for r in rows if r["brand_id"] in confirmed))
    return rows
