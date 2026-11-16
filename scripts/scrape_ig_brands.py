
import os
import time
import random
import pandas as pd
import instaloader
from tqdm import tqdm

# ==========================
# 1. Paths
# ==========================

BASE_DIR = "/home/yingtzudai/Multimodal_SPoD_Video/data"

INPUT_PATH = os.path.join(BASE_DIR, "brands_in_database.csv")
OUTPUT_PATH = os.path.join(BASE_DIR, "ig_brand_info.csv")

# ==========================
# 2. Configuration
# ==========================

SAVE_EVERY = 10
MIN_DELAY = 8
MAX_DELAY = 15

# Set to 10 for testing, None for all accounts
MAX_ACCOUNTS = 10

# ==========================
# 3. Initialize Instaloader
# ==========================

L = instaloader.Instaloader(
    download_pictures=False,
    download_videos=False,
    download_video_thumbnails=False,
    download_geotags=False,
    download_comments=False,
    save_metadata=False,
    compress_json=False,
    quiet=True,
    max_connection_attempts=1
)

# ==========================
# 4. Load brand usernames
# ==========================

df = pd.read_csv(INPUT_PATH, dtype={"brand_id": str})

if "brand_id" not in df.columns:
    raise ValueError("Column 'brand_id' not found")

brands = (
    df["brand_id"]
    .dropna()
    .astype(str)
    .str.strip()
    .str.lstrip("@")
)

brands = brands[brands != ""].drop_duplicates().tolist()

print(f"Total unique brands: {len(brands)}")

# ==========================
# 5. Resume existing results
# ==========================

columns = [
    "brand_id",
    "username",
    "name",
    "followers",
    "followees",
    "posts",
    "business_category",
    "is_business_account",
    "status"
]

if os.path.exists(OUTPUT_PATH):
    results_df = pd.read_csv(OUTPUT_PATH, dtype={"brand_id": str})
    results_df = results_df.reindex(columns=columns)
else:
    results_df = pd.DataFrame(columns=columns)

completed = set(
    results_df.loc[
        results_df["status"] == "success", "brand_id"
    ].dropna().str.lower()
)

pending = [
    brand for brand in brands
    if brand.lower() not in completed
]

if MAX_ACCOUNTS is not None:
    pending = pending[:MAX_ACCOUNTS]

print(f"Already completed: {len(completed)}")
print(f"Accounts to scrape: {len(pending)}")

# ==========================
# 6. Scrape profile metadata
# ==========================

for i, brand in enumerate(tqdm(pending, desc="Scraping brands"), 1):

    try:
        profile = instaloader.Profile.from_username(
            L.context, brand
        )

        result = {
            "brand_id": brand,
            "username": profile.username,
            "name": profile.full_name,
            "followers": profile.followers,
            "followees": profile.followees,
            "posts": profile.mediacount,
            "business_category": profile.business_category_name,
            "is_business_account": profile.is_business_account,
            "status": "success"
        }

    except Exception as e:

        error_type = type(e).__name__

        print(f"\nFailed: {brand} ({error_type}: {e})")

        result = {
            "brand_id": brand,
            "username": None,
            "name": None,
            "followers": None,
            "followees": None,
            "posts": None,
            "business_category": None,
            "is_business_account": None,
            "status": error_type
        }

        if isinstance(
            e,
            (instaloader.exceptions.TooManyRequestsException,
             instaloader.exceptions.LoginRequiredException)
        ):
            print("Rate limit or login required. Stopping.")
            results_df = results_df[
                results_df["brand_id"].str.lower() != brand.lower()
            ]
            results_df = pd.concat(
                [results_df, pd.DataFrame([result])],
                ignore_index=True
            )
            results_df.to_csv(
                OUTPUT_PATH, index=False, encoding="utf-8-sig"
            )
            break

    # Replace previous result for this brand
    results_df = results_df[
        results_df["brand_id"].str.lower() != brand.lower()
    ]

    results_df = pd.concat(
        [results_df, pd.DataFrame([result])],
        ignore_index=True
    )

    # Periodic save
    if i % SAVE_EVERY == 0:
        results_df.to_csv(
            OUTPUT_PATH, index=False, encoding="utf-8-sig"
        )

    # Request delay
    if i < len(pending):
        time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))

# ==========================
# 7. Final save
# ==========================

results_df.to_csv(
    OUTPUT_PATH,
    index=False,
    encoding="utf-8-sig"
)

print("\nScraping finished")
print(f"Output: {OUTPUT_PATH}")
print(f"Total records: {len(results_df)}")

print("\nStatus summary:")
print(results_df["status"].value_counts(dropna=False))
