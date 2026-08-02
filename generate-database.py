import pandas as pd
import sqlite3
import json
import os
from datetime import datetime, timezone

os.chdir("db_build")

# Rename the database file
os.rename("cache.sqlite3", f"jlcpcb-components.sqlite3")

initial_db_size = os.path.getsize("jlcpcb-components.sqlite3")
print(f"Initial SQLite Database Size: {initial_db_size / (1024 ** 3):.2f} GiB")

conn = sqlite3.connect("jlcpcb-components.sqlite3")
cur = conn.cursor()

cur.execute("PRAGMA journal_mode = WAL")  # Enable Write-Ahead Logging (WAL) for improved performance and concurrency
cur.execute("PRAGMA synchronous = NORMAL")  # Set the synchronous mode to NORMAL, which balances safety and performance
cur.execute("PRAGMA temp_store = MEMORY")  # Store temporary tables and indices in memory for faster access
cur.execute("PRAGMA mmap_size = 536870912")  # Set the maximum memory map size to 512MiB


# --- Schema detection ---------------------------------------------------
# yaqwsx/jlcparts migrated their published cache.sqlite3 to a new
# "source-db-v2" schema (tables `jlc_components` / `lcsc_components`,
# tracked via a `meta` table) at some point, replacing the old flat
# `components` table / `v_components` view this script originally read
# from. This broke this script with "no such table: components" once the
# old schema stopped being published. This block detects whichever schema
# we actually got and normalizes it into the exact same output shape this
# script always produced, so nothing downstream (JLCPCB-Kicad-Library's
# libraryCreatorScript.py) has to change.
def _detect_schema(cur):
    try:
        cur.execute("SELECT value FROM meta WHERE key = 'format' LIMIT 1")
        row = cur.fetchone()
        if row and row[0] == "source-db-v2":
            return "v2"
    except sqlite3.OperationalError:
        pass
    return "legacy"


schema = _detect_schema(cur)
print(f"Detected source database schema: {schema}")

if schema == "v2":
    # Delete components with low stock
    cur.execute("DELETE FROM jlc_components WHERE stock < 5;")
    conn.commit()
    print(f"Deleted {cur.rowcount} components with low stock")

    # Create an FTS (Full-Text Search) index on multiple columns (helps to speed up searching the database)
    cur.execute("DROP TABLE IF EXISTS jlc_components_fts;")
    cur.execute(
        """
        CREATE VIRTUAL TABLE jlc_components_fts USING fts5(
            lcsc,
            mfr,
            package,
            description,
            datasheet,
            content='jlc_components'
        );
    """
    )
    conn.commit()
else:
    # Delete components with low stock
    cur.execute("DELETE FROM components WHERE stock < 5;")
    conn.commit()
    print(f"Deleted {cur.rowcount} components with low stock")

    # Create an FTS (Full-Text Search) index on multiple columns (helps to speed up searching the database)
    cur.execute(
        """
        CREATE VIRTUAL TABLE components_fts USING fts5(
            lcsc,
            mfr,
            package,
            description,
            datasheet,
            content='components'
        );
    """
    )
    conn.commit()

# Reindex database to reduce file size
cur.execute("REINDEX;")
conn.commit()

# Vacuum database to reduce file size
cur.execute("VACUUM;")
conn.commit()

# --- Preferred-parts correction (legacy schema only) ---------------------
# The v2 schema already tracks `preferred` natively -- yaqwsx's own pipeline
# runs `jlcparts updatepreferred` (pulling JLC's real preferred-parts list)
# before this file is ever published -- so this manual correction pass,
# which used to *infer* "preferred" from how recently a part was scraped,
# is redundant now and only runs against the old schema.
if schema == "legacy":
    file_location = os.path.join("..", os.path.join("scraped", "ComponentList.csv"))
    df = pd.read_csv(file_location)

    # Convert date columns to datetime with UTC timezone
    df["First Seen"] = pd.to_datetime(df["First Seen"], format="%Y/%m/%d", utc=True)
    df["Last Seen"] = pd.to_datetime(df["Last Seen"], format="%Y/%m/%d", utc=True)

    # Calculate time differences
    now = datetime.now(timezone.utc)
    df["Days Since First Seen"] = (now - df["First Seen"]).dt.days
    df["Days Since Last Seen"] = (now - df["Last Seen"]).dt.days

    # Filter components
    component_codes = df[(df["Days Since First Seen"] >= 1) & (df["Days Since Last Seen"] < 2)]["lcsc"].astype(int).tolist()

    preferred_parts_corrected = 0
    for code in component_codes:
        cur.execute("SELECT 1 FROM components WHERE lcsc = ?", (code,))
        if cur.fetchone():
            cur.execute(
                "UPDATE components SET preferred = 1 WHERE lcsc = ? AND basic = 0 AND preferred = 0",
                (code,),
            )
            conn.commit()
            preferred_parts_corrected += 1

    print(f"Preferred Parts Corrected: {preferred_parts_corrected}")

optimized_db_size = os.path.getsize("jlcpcb-components.sqlite3")
print(f"Optimized Database Size: {optimized_db_size / (1024 ** 3):.2f} GiB")

# --- Retrieve basic/preferred components ($0 for loading feeders), exclude "0201" package ---
if schema == "v2":
    # Debug: raw counts straight from the DB, before any filtering, so we can
    # tell whether a low "preferred" count comes from upstream (yaqwsx's own
    # `jlcparts updatepreferred` step not fully populating the flag) versus
    # a mistake in the filtering/join below. Compare these against JLCPCB's
    # live site counts at https://jlcpcb.com/parts/basic_parts.
    cur.execute("SELECT COUNT(*) FROM jlc_components")
    print(f"[debug] total jlc_components rows (post stock<5 delete): {cur.fetchone()[0]}")
    cur.execute("SELECT COUNT(*) FROM jlc_components WHERE library_type = 'base'")
    print(f"[debug] rows with library_type = 'base': {cur.fetchone()[0]}")
    cur.execute("SELECT COUNT(*) FROM jlc_components WHERE preferred = 1")
    print(f"[debug] rows with preferred = 1: {cur.fetchone()[0]}")
    cur.execute("SELECT COUNT(*) FROM jlc_components WHERE preferred = 1 AND package != '0201'")
    print(f"[debug] rows with preferred = 1 AND package != '0201': {cur.fetchone()[0]}")
    cur.execute("SELECT key, value FROM meta")
    for row in cur.fetchall():
        print(f"[debug] meta.{row[0]} = {row[1]}")

    cur.execute(
        """
        SELECT
            j.lcsc, j.category, j.subcategory, j.mfr, j.package, j.joints,
            j.manufacturer, j.library_type, j.preferred, j.description,
            j.datasheet, j.stock, j.price AS price_csv,
            l.attributes AS lcsc_attributes
        FROM jlc_components j
        LEFT JOIN lcsc_components l ON l.lcsc = j.lcsc
        WHERE (j.library_type = 'base' OR j.preferred = 1) AND j.package != '0201';
    """
    )
    columns = [d[0] for d in cur.description]
    df_sorted = pd.DataFrame(cur.fetchall(), columns=columns)

    def _price_csv_to_json(price_csv):
        # The old schema stored price as a JSON list of tiers, e.g.
        # [{"price": 0.0123}, ...], and downstream code (libraryCreatorScript.py)
        # only ever reads the first tier (price_json[0]["price"]). The new
        # schema stores price as "qFrom-qTo:price,qFrom2-qTo2:price2,...".
        # This reconstructs just enough of the old shape to stay compatible.
        if not price_csv:
            return "[]"
        try:
            first_tier = price_csv.split(",")[0]
            _, price_str = first_tier.split(":")
            return json.dumps([{"price": float(price_str)}])
        except Exception:
            return "[]"

    def _extra_json(lcsc_attrs_json):
        try:
            attrs = json.loads(lcsc_attrs_json) if lcsc_attrs_json else {}
            if not isinstance(attrs, dict):
                attrs = {}
        except Exception:
            attrs = {}
        return json.dumps({"attributes": attrs})

    df_sorted = pd.DataFrame(
        {
            "lcsc": df_sorted["lcsc"],
            "category_id": 0,  # unused downstream; kept only for column-shape compatibility
            "category": df_sorted["category"],
            "subcategory": df_sorted["subcategory"],
            "mfr": df_sorted["mfr"],
            "manufacturer": df_sorted["manufacturer"],
            "package": df_sorted["package"],
            "joints": df_sorted["joints"],
            "basic": (df_sorted["library_type"] == "base").astype(int),
            "preferred": df_sorted["preferred"].astype(int),
            "description": df_sorted["description"],
            "datasheet": df_sorted["datasheet"],
            "stock": df_sorted["stock"],
            "last_on_stock": 0,  # unused downstream; kept only for column-shape compatibility
            "price": df_sorted["price_csv"].apply(_price_csv_to_json),
            "extra": df_sorted["lcsc_attributes"].apply(_extra_json),
        }
    )
else:
    cur.execute(
        """
        SELECT * FROM v_components
        WHERE (basic > 0 OR preferred > 0) AND package != '0201';
    """
    )
    filtered_components = cur.fetchall()

    # Create Pandas DataFrame
    df_sorted = pd.DataFrame(filtered_components, columns=[desc[0] for desc in cur.description])

# Merge assembly details
file_location = os.path.join("..", os.path.join("scraped", "assembly-details.csv"))
df = pd.read_csv(file_location)

df_filtered = df[df["lcsc"].isin(df_sorted["lcsc"])]

df_sorted = pd.merge(
    df_sorted, df_filtered[["lcsc", "Assembly Process", "Min Order Qty", "Attrition Qty"]], on="lcsc", how="right"
)

df_sorted = df_sorted.sort_values(by=["category", "subcategory", "package"])

# Remove parts with missing price fields
df_sorted = df_sorted.drop(df_sorted[df_sorted["price"] == "[]"].index)

# Save sorted DataFrame to CSV
df_sorted.to_csv("jlcpcb-components-basic-preferred.csv", index=False, header=True)

cur.execute("PRAGMA analyze")  # Update statistics for the query planner to improve query performance
cur.execute("PRAGMA optimize")  # Perform various optimizations, such as reindexing and refreshing views
conn.close()
