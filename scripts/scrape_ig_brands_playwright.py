"""Collect Instagram profile counts from public HTML, a signed-in browser, or saved pages."""

import argparse
import csv
import json
import os
import re
import sys
import tempfile
import time
import unicodedata
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

DATA = Path(__file__).resolve().parents[1] / "data"
BROWSER_STATE = DATA / ".ig_browser_state.json"
HANDLE = re.compile(r"[a-z0-9_.]{1,30}", re.ASCII)
NUMBER = r"(?:[0-9]{1,3}(?:[, ][0-9]{3})+|[0-9]+)(?:\.[0-9]+)?\s*[kKmMbB萬万億亿]?"
MULTIPLIER = dict(k=1000, m=1000000, b=1000000000, 萬=10000, 万=10000, 億=100000000, 亿=100000000)
LABELS = {
    "followers": r"followers?|位粉[絲丝]|粉[絲丝]",
    "followees": r"following|追[蹤踪]中|人追[蹤踪]中|关注",
    "posts": r"posts?|篇[貼贴]文|[貼贴]文|帖子",
}
COUNT_KEYS = {
    "followers": ("follower_count", "followers_count", "edge_followed_by"),
    "followees": ("following_count", "follows_count", "edge_follow"),
    "posts": ("all_media_count", "media_count", "edge_owner_to_timeline_media"),
}
CATEGORY_KEYS = ("business_category_name", "category_name", "category_label", "category")
PROFILE_KEYS = set(CATEGORY_KEYS).union(*COUNT_KEYS.values())
FIELDS = (
    "brand_id", "followers", "followees", "posts", "brand_category",
    *(f"{metric}_{suffix}" for metric in LABELS for suffix in ("raw", "is_exact", "source")),
    "category_source", "scraped_at", "status", "detail", "apify_status",
)
APIFY_COUNTS = {"followers": "followersCount", "followees": "followsCount", "posts": "postsCount"}
INSTAGRAM_HOSTS = {"instagram.com", "www.instagram.com"}
INSTAGRAM = "https://www.instagram.com"
STOP_STATUSES = {"login_required", "restricted", "rate_limited", "request_error"}


def normalize_handle(value):
    return (value or "").strip().lstrip("@").lower()


def normalized_text(value):
    return unicodedata.normalize("NFKC", value)


def parse_count(raw):
    value = normalized_text(raw).strip().lower()
    if not re.fullmatch(NUMBER, value):
        return None, None
    unit = value[-1] if value[-1] in MULTIPLIER else ""
    number = (value[:-1] if unit else value).replace(",", "").replace(" ", "")
    if "." in number and not unit:
        return None, None
    return round(Decimal(number) * MULTIPLIER.get(unit, 1)), not unit


def profile_handle(url):
    parsed = urlsplit(url)
    if parsed.hostname not in INSTAGRAM_HOSTS:
        return None
    handle = normalize_handle(parsed.path.strip("/"))
    return handle if HANDLE.fullmatch(handle) else None


class ProfileHTML(HTMLParser):
    def __init__(self):
        super().__init__()
        self.meta = {}
        self.canonical = ""
        self.scripts = []
        self.json_script = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "meta" and attrs.get("content"):
            key = attrs.get("property") or attrs.get("name")
            self.meta.setdefault(key, attrs["content"])
        elif tag == "link" and attrs.get("rel") == "canonical":
            self.canonical = attrs.get("href") or ""
        elif tag == "script":
            self.json_script = attrs.get("type") == "application/json"

    def handle_endtag(self, tag):
        if tag == "script":
            self.json_script = False

    def handle_data(self, data):
        if self.json_script and '"username"' in data:
            self.scripts.append(data)


def profile_objects(scripts, brand):
    for script in scripts:
        try:
            pending = [json.loads(script)]
        except (ValueError, RecursionError):
            continue
        while pending:
            value = pending.pop()
            if isinstance(value, dict):
                username = value.get("username")
                if isinstance(username, str) and username.lower() == brand and PROFILE_KEYS.intersection(value):
                    yield value
                pending.extend(reversed(value.values()))
            elif isinstance(value, list):
                pending.extend(reversed(value))


def structured_count(profile, metric):
    for key in COUNT_KEYS[metric]:
        count = profile.get(key)
        if isinstance(count, dict):
            count = count.get("count")
        if type(count) is int and count >= 0:
            return count
    return None


def has_counts(row):
    return all(row.get(key) not in (None, "") for key in LABELS)


def count_from_text(text, metric):
    match = re.search(
        rf"(?<![\w.,])({NUMBER})\s*(?:{LABELS[metric]})(?![a-z])",
        normalized_text(text), re.I,
    )
    return match[1].strip() if match else ""


def extract_profile(document, brand, source):
    page = ProfileHTML()
    page.feed(document)
    page.close()
    profiles = list(profile_objects(page.scripts, brand))
    og_url = page.meta.get("og:url") or page.canonical
    description = page.meta.get("og:description") or page.meta.get("description") or ""
    description_handle = re.search(r"\(@([a-z0-9_.]+)\)\s*$", description, re.I | re.ASCII)
    identity = profile_handle(og_url)
    if not identity and description_handle:
        identity = description_handle[1].lower()
    if identity and identity != brand:
        return {"status": "profile_mismatch", "detail": f"HTML identifies @{identity}"}
    if not identity and not profiles:
        return {"status": "profile_unreadable", "detail": "No matching profile metadata"}

    # The counts precede the display name or biography in the profile description.
    summary = description.split(" - ", 1)[0]
    result = {}
    for metric in LABELS:
        count = next(
            (count for profile in profiles if (count := structured_count(profile, metric)) is not None),
            None,
        )
        if count is not None:
            raw, exact, method = str(count), True, "json"
        else:
            raw = count_from_text(summary, metric)
            count, exact = parse_count(raw)
            method = "meta"
        if count is not None:
            result.update({
                metric: count,
                f"{metric}_raw": raw,
                f"{metric}_is_exact": exact,
                f"{metric}_source": f"{source}_{method}",
            })

    for profile in profiles:
        category = next(
            (profile[key].strip() for key in CATEGORY_KEYS
             if isinstance(profile.get(key), str) and profile[key].strip()),
            "",
        )
        if category:
            result.update(brand_category=category, category_source=f"{source}_json")
            break

    if not result:
        return {"status": "profile_unreadable", "detail": "Matching profile has no supported fields"}
    return result


class InstagramRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        destination = urlsplit(newurl)
        if destination.scheme != "https" or destination.hostname not in INSTAGRAM_HOSTS:
            raise HTTPError(req.full_url, 403, "Unexpected profile redirect", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def navigation_issue(url, brand=None):
    path = urlsplit(url).path
    if path.startswith(("/accounts/login", "/accounts/signup")):
        return {"status": "login_required", "detail": "Redirected to Instagram login or signup"}
    if path.startswith(("/challenge", "/checkpoint", "/accounts/suspended", "/auth_platform/recaptcha")):
        return {"status": "restricted", "detail": "Redirected to an Instagram restriction page"}
    if brand and profile_handle(url) != brand:
        return {"status": "profile_mismatch", "detail": "Redirected away from the requested handle"}
    return None


def http_failure(code, retry_after=None):
    status = {
        401: "login_required", 403: "restricted", 404: "not_found",
        410: "not_found", 429: "rate_limited",
    }.get(code, "request_error")
    detail = f"HTTP {code}"
    if retry_after:
        detail += f" (Retry-After: {retry_after})"
    return {"status": status, "detail": detail}


def fetch_profile(opener, brand):
    request = Request(
        f"{INSTAGRAM}/{brand}/?hl=en",
        headers={"Accept": "text/html", "Accept-Language": "en-US,en;q=0.9"},
    )
    try:
        with opener.open(request, timeout=20) as response:
            if issue := navigation_issue(response.url, brand):
                return issue
            document = response.read().decode(response.headers.get_content_charset() or "utf-8", errors="replace")
    except HTTPError as error:
        issue = http_failure(error.code, error.headers.get("Retry-After") if error.headers else None)
        error.close()
        return issue
    except (URLError, OSError) as error:
        return {"status": "request_error", "detail": str(error)}
    return extract_profile(document, brand, "public_html")


@contextmanager
def instagram_browser(state=None):
    try:
        from playwright.sync_api import Error, sync_playwright
    except ImportError as error:
        raise RuntimeError("Install Playwright in this Python environment to use browser mode.") from error
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=state is not None)
            try:
                context = browser.new_context(locale="en-US", storage_state=state)
                yield context
            finally:
                browser.close()
    except Error as error:
        raise RuntimeError(str(error).splitlines()[0]) from error


def wait_for_browser_login(context, timeout=600):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        cookies = context.cookies(INSTAGRAM)
        if not context.pages:
            raise ValueError("The sign-in window was closed. Run --login to try again.")
        if any(cookie["name"] == "sessionid" and cookie["value"] for cookie in cookies):
            for page in context.pages:
                url = page.url
                location = urlsplit(url)
                if (
                    location.hostname in INSTAGRAM_HOSTS
                    and not location.path.startswith(("/accounts/two_factor", "/accounts/onetap"))
                    and not navigation_issue(url)
                ):
                    return cookies
        time.sleep(1)
    raise ValueError("Instagram sign-in did not finish within 10 minutes. Complete verification and try again.")


def save_browser_login(destination):
    if sys.platform.startswith("linux") and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        raise ValueError("Run --login on a computer with a graphical desktop, then use --browser-state.")
    with instagram_browser() as context:
        page = context.new_page()
        page.goto(f"{INSTAGRAM}/accounts/login/", wait_until="domcontentloaded", timeout=30000)
        print("Sign in to Instagram in the browser. Waiting up to 10 minutes; no need to press Enter.", flush=True)
        cookies = wait_for_browser_login(context)
        state = context.storage_state()
        state["cookies"] = cookies
        state["origins"] = [
            entry for entry in state["origins"] if urlsplit(entry["origin"]).hostname in INSTAGRAM_HOSTS
        ]
        destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(dir=destination.parent)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(state, stream)
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)
    print(f"Saved browser session to {destination}")


def fetch_browser_profile(page, brand):
    from playwright.sync_api import Error, TimeoutError

    try:
        response = page.goto(f"{INSTAGRAM}/{brand}/?hl=en", wait_until="domcontentloaded", timeout=30000)
        if issue := navigation_issue(page.url):
            return issue
        if response and response.status >= 400:
            return http_failure(response.status, response.headers.get("retry-after"))
        if issue := navigation_issue(page.url, brand):
            return issue

        header = page.locator("main header").first
        try:
            header.wait_for(state="visible", timeout=7000)
        except TimeoutError:
            pass  # Some profiles only expose counts in page metadata.

        if issue := navigation_issue(page.url, brand):
            return issue
        if page.locator("input[type=password]").count():
            return {"status": "login_required", "detail": "Instagram displayed a sign-in form"}

        result = extract_profile(page.content(), brand, "browser")
        if result.get("status") == "profile_mismatch":
            return result

        # The account's own follower/following links identify its profile stats.
        followers = header.locator(f'a[href="/{brand}/followers/"]').first
        followees = header.locator(f'a[href="/{brand}/following/"]').first
        if followers.count() and followees.count():
            stats = followers.locator("xpath=ancestor::ul[1]")
            if stats.count():
                text = stats.inner_text(timeout=2000)
                for metric in LABELS:
                    if metric not in result:
                        raw = count_from_text(text, metric)
                        count, exact = parse_count(raw)
                        if count is not None:
                            result.update({
                                metric: count, f"{metric}_raw": raw, f"{metric}_is_exact": exact,
                                f"{metric}_source": "browser_header",
                            })
                if any(metric in result for metric in LABELS):
                    result.pop("status", None)
                    result.pop("detail", None)
        return result
    except Error as error:
        return {"status": "request_error", "detail": str(error).splitlines()[0]}


def read_rows(path, required):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        fields = tuple(reader.fieldnames or ())
        if not set(required).issubset(fields):
            raise ValueError(f"{path} must contain {', '.join(required)}")
        return fields, list(reader)


def read_categories(path):
    fields, rows = read_rows(path, ())
    if {"brand_id", "brand_category"}.issubset(fields):
        handle_field, category_field, source = "brand_id", "brand_category", "provided_csv"
    elif {"username", "businessCategoryName"}.issubset(fields):
        handle_field, category_field, source = "username", "businessCategoryName", "apify_csv"
    else:
        raise ValueError(f"{path} must contain brand_id and brand_category or username and businessCategoryName")
    categories = {}
    for row in rows:
        brand = normalize_handle(row[handle_field])
        category = (row[category_field] or "").strip()
        if brand and category:
            if brand in categories and categories[brand] != category:
                raise ValueError(f"{path} contains conflicting categories for @{brand}")
            categories[brand] = category
    return categories, source


def save_results(path, fields, results):
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8-sig", newline="", dir=path.parent, delete=False,
    ) as stream:
        temporary = Path(stream.name)
        try:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            writer.writerows(results.values())
            stream.flush()
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def merge_result(previous, acquired):
    # Keep a previously acquired value when this source has it missing.
    result = {**previous, **acquired}
    if "status" not in acquired and (
        any(metric in acquired for metric in LABELS)
        or previous.get("status") in (None, "", "success", "partial")
    ):
        result["status"] = "success" if has_counts(result) and result.get("brand_category") else "partial"
        result["detail"] = ""
    return result


def export_apify_input(args, brands, results):
    pending = []
    for brand in brands:
        row = results.get(brand, {})
        if args.refresh:
            pending.append(brand)
            continue
        if row.get("apify_status") == "not_found":
            continue
        if not has_counts(row) or (
            args.include_missing_category and not row.get("brand_category") and row.get("apify_status") != "success"
        ):
            pending.append(brand)
    json.dump({"usernames": pending[:args.limit], "includeAboutSection": False}, sys.stdout, indent=2)
    print()
    return 0


def import_apify_results(path, output, fields, brands, results):
    headers, incoming = read_rows(path, ("username",))
    if not set(APIFY_COUNTS.values()).issubset(headers) and "error" not in headers:
        raise ValueError(f"{path} must contain the Apify counts or error columns")
    allowed = set(brands)
    changed = set()
    not_found = set()
    unmatched = 0
    for record in incoming:
        brand = normalize_handle(record["username"])
        if brand not in allowed:
            unmatched += 1
            continue
        previous = results.get(brand, {"brand_id": brand})
        acquired = {}
        error = (record.get("error") or "").strip().lower()
        if error == "not_found":
            not_found.add(brand)
            if previous.get("apify_status") not in {"success", "not_found"}:
                results[brand] = {**previous, "apify_status": "not_found"}
                changed.add(brand)
            continue
        if error:
            continue
        has_provider_data = False
        for metric, column in APIFY_COUNTS.items():
            raw = (record.get(column) or "").strip()
            count, exact = parse_count(raw)
            if count is not None:
                has_provider_data = True
                if previous.get(metric) in (None, ""):
                    acquired.update({
                        metric: count, f"{metric}_raw": raw, f"{metric}_is_exact": exact,
                        f"{metric}_source": "apify_csv",
                    })
        category = (record.get("businessCategoryName") or "").strip()
        if category and category.lower() != "null":
            has_provider_data = True
            if not previous.get("brand_category"):
                acquired.update(brand_category=category, category_source="apify_csv")
        if has_provider_data and (acquired or previous.get("apify_status") != "success"):
            results[brand] = merge_result(previous, acquired)
            results[brand]["apify_status"] = "success"
            changed.add(brand)
    if changed:
        save_results(output, fields, results)
    with_counts = sum(has_counts(results[brand]) for brand in changed)
    with_category = sum(bool(results[brand].get("brand_category")) for brand in changed)
    print(
        f"Updated profiles: {len(changed)} | With all counts: {with_counts} | "
        f"With a category: {with_category} | Apify not found: {len(not_found)} | "
        f"Unmatched CSV rows: {unmatched} | Output: {output}"
    )
    return 0


def collect(args, brands):
    fields, existing = (
        read_rows(args.output, ("brand_id",))
        if args.output.exists() and args.output.stat().st_size else ((), [])
    )
    fields = tuple(dict.fromkeys((*FIELDS, *fields)))
    results = {normalize_handle(row["brand_id"]): row for row in existing if normalize_handle(row["brand_id"])}
    if args.export_apify:
        return export_apify_input(args, brands, results)
    if args.import_apify:
        return import_apify_results(args.import_apify, args.output, fields, brands, results)
    updated = False
    for row in results.values():
        if not row.get("brand_category") and row.get("business_category"):
            row.update(brand_category=row["business_category"], category_source="previous_csv")
            updated = True
    if args.categories:
        categories, source = read_categories(args.categories)
        for brand in brands:
            if brand in categories:
                acquired = {"brand_category": categories[brand], "category_source": source}
                results[brand] = merge_result(results.get(brand, {"brand_id": brand}), acquired)
                updated = True
    if updated:
        save_results(args.output, fields, results)

    pending = []
    missing = 0
    for brand in brands:
        row = results.get(brand, {})
        if args.offline:
            if not args.refresh and has_counts(row) and row.get("brand_category"):
                continue
            if not (args.html_dir / f"{brand}.html").is_file():
                missing += 1
                continue
        elif not args.refresh and (has_counts(row) or row.get("status") == "not_found"):
            continue
        pending.append(brand)

    if not (args.offline or args.refresh or args.brand):
        # Try new profiles first so a previously blocked profile cannot stall every batch.
        pending.sort(key=results.__contains__)
    pending = pending[:args.limit]
    print(f"Brand IDs: {len(brands)} | This run: {len(pending)} | Missing saved HTML: {missing}", flush=True)
    if args.browser_state and pending:
        with instagram_browser(args.browser_state) as context:
            if not any(cookie["name"] == "sessionid" for cookie in context.cookies(INSTAGRAM)):
                raise ValueError("The browser state has no active Instagram session. Run --login again.")
            page = context.new_page()
            return collect_batch(args, fields, results, pending, lambda brand: fetch_browser_profile(page, brand))
    opener = build_opener(InstagramRedirects())
    return collect_batch(args, fields, results, pending, lambda brand: fetch_profile(opener, brand))


def collect_batch(args, fields, results, pending, fetch):
    statuses = Counter()
    unreadable = 0
    stopped = False
    for index, brand in enumerate(pending):
        previous = results.get(brand, {"brand_id": brand})
        if args.offline:
            path = args.html_dir / f"{brand}.html"
            try:
                acquired = extract_profile(path.read_text(encoding="utf-8-sig", errors="replace"), brand, "saved_html")
            except OSError as error:
                acquired = {"status": "read_error", "detail": str(error)}
            if not args.refresh and has_counts(previous) and "status" not in acquired:
                acquired = {key: acquired[key] for key in ("brand_category", "category_source") if key in acquired}
        else:
            acquired = fetch(brand)

        if any(metric in acquired for metric in LABELS):
            acquired["scraped_at"] = datetime.now(timezone.utc).isoformat()
        row = merge_result(previous, acquired)
        results[brand] = row
        save_results(args.output, fields, results)

        status = row["status"]
        statuses[status] += 1
        detail = f" ({row['detail']})" if row.get("detail") else ""
        print(f"{index + 1}/{len(pending)} @{brand}: {status}{detail}", flush=True)
        unreadable = unreadable + 1 if status == "profile_unreadable" else 0
        if not args.offline:
            if status in STOP_STATUSES or unreadable >= 3:
                print("Stopped the online batch. Results were saved through the last attempted profile.", file=sys.stderr)
                if status == "login_required":
                    suggestion = (
                        "Refresh the browser session with --login before resuming."
                        if args.browser_state else "Use --login and --browser-state to resume with a signed-in browser."
                    )
                    print(suggestion, file=sys.stderr)
                stopped = True
                break
            if index + 1 < len(pending):
                time.sleep(args.delay)

    print(f"Summary: {dict(statuses)} | Output: {args.output}")
    return int(stopped)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DATA / "brands_in_database.csv")
    parser.add_argument("--output", type=Path, default=DATA / "ig_brand_info.csv")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--offline", action="store_true", help="Read saved HTML without accessing the network.")
    source.add_argument("--login", type=Path, nargs="?", const=BROWSER_STATE, metavar="PATH",
                        help="Sign in manually in a graphical Chromium window and save the local session.")
    source.add_argument("--browser-state", type=Path, nargs="?", const=BROWSER_STATE, metavar="PATH",
                        help="Fetch profiles through Chromium using a session saved with --login.")
    source.add_argument("--export-apify", action="store_true", help="Print Apify input JSON for missing profiles.")
    source.add_argument("--import-apify", type=Path, metavar="CSV", help="Fill missing values from an Apify profile CSV.")
    parser.add_argument("--html-dir", type=Path, default=DATA / "ig_profile_html")
    parser.add_argument(
        "--categories", type=Path,
        help="Supplement categories from a brand_id/brand_category CSV or an Apify profile CSV.",
    )
    parser.add_argument("--brand", action="append", help="Process a handle from the input. Repeat for more handles.")
    parser.add_argument("--limit", type=int, help="Maximum number of profiles to attempt.")
    parser.add_argument("--delay", type=float, default=8, help="Seconds between online requests (default: 8).")
    parser.add_argument("--refresh", action="store_true", help="Recheck profiles that already have results.")
    parser.add_argument("--include-missing-category", action="store_true",
                        help="Include profiles with complete counts but no category in an Apify export.")
    args = parser.parse_args()
    if (args.limit is not None and args.limit < 1) or not (0 <= args.delay < float("inf")):
        parser.error("--limit must be positive and --delay must be finite and nonnegative")
    if any(path and path.resolve() == args.output.resolve() for path in (
        args.input, args.categories, args.browser_state, args.login, args.import_apify,
    )):
        parser.error("--output must differ from input and browser state paths")
    if args.include_missing_category and not args.export_apify:
        parser.error("--include-missing-category requires --export-apify")

    try:
        if args.login:
            if args.login.suffix.lower() != ".json":
                parser.error("--login needs a .json path for the browser state")
            save_browser_login(args.login)
            return 0
        if args.browser_state and not args.browser_state.is_file():
            parser.error(f"Browser state not found at {args.browser_state}. Run --login first.")

        import fcntl

        _, rows = read_rows(args.input, ("brand_id",))
        handles = dict.fromkeys(normalize_handle(row["brand_id"]) for row in rows)
        brands = [brand for brand in handles if HANDLE.fullmatch(brand) and brand not in {".", ".."}]
        invalid = [handle for handle in handles if handle and handle not in brands]
        if invalid:
            print(f"Skipped {len(invalid)} invalid brand IDs.", file=sys.stderr)
        if args.brand:
            selected = list(dict.fromkeys(map(normalize_handle, args.brand)))
            if unknown := set(selected).difference(brands):
                parser.error(f"--brand values not in input: {', '.join(sorted(unknown))}")
            brands = selected
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.with_name(args.output.name + ".lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                parser.exit(1, f"Another scraper is writing {args.output}\n")
            return collect(args, brands)
    except (EOFError, OSError, RuntimeError, ValueError, csv.Error) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    sys.exit(main())
