# This script fetches a list of components from JLCPCB's API, updates a local CSV file with new components,
# and records the "First Seen" and "Last Seen" dates for each component. It is scheduled to run daily via GitHub Actions.

import os
import requests
import re
import csv
import time
import random
from datetime import datetime, timezone

# Get the current date in UTC
today_date_str = datetime.now(tz=timezone.utc).strftime("%Y/%m/%d")

# URL for fetching component list from JLCPCB API
url = "https://jlcpcb.com/api/overseas-pcb-order/v1/shoppingCart/smtGood/selectSmtComponentList/v2"

# Headers for the HTTP GET request
headers = {
    "Host": "jlcpcb.com",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:130.0) Gecko/20100101 Firefox/130.0",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-GB,en;q=0.5",
    "Accept-Encoding": "gzip, deflate, br",
    "Content-Type": "application/json",
    "Origin": "https://jlcpcb.com",
    "DNT": "1",
    "Connection": "keep-alive",
    "Referer": "https://jlcpcb.com/parts/basic_parts",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
    "Priority": "u=0",
    "Pragma": "no-cache",
    "Cache-Control": "no-cache",
}


def update_component(components, lcsc_code):
    """
    Update the component list with the given LCSC code.
    If the component already exists, update the 'Last Seen' field.
    If the component does not exist, add it with 'First Seen' and 'Last Seen' set to today's date.
    """
    for component in components:
        if component["lcsc"] == lcsc_code:
            component["Last Seen"] = today_date_str
            return False

    # Component doesn't exist, add it
    new_component = {"lcsc": lcsc_code, "First Seen": today_date_str, "Last Seen": today_date_str}
    components.append(new_component)
    return True


def get_part_data(lcsc_number: int, retries: int = 3, retry_delay: float = 5.0) -> dict:
    """Get data for a given LCSC number from the API.

    Hardened against transient failures: JLCPCB's cart API will occasionally
    reject a request mid-handshake (seen as ssl.SSLError:
    TLSV1_ALERT_INTERNAL_ERROR) when hit with many requests back-to-back and
    no delay between them, which used to crash the whole script and block
    the downstream database-build job entirely. This retries with backoff
    and always returns a {"success": False, ...} dict instead of letting a
    network/SSL exception propagate.
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/58.0.3029.110 Safari/537.3"
    }

    last_error = None
    for attempt in range(1, retries + 1):
        try:
            response = requests.get(
                f"https://cart.jlcpcb.com/shoppingCart/smtGood/getComponentDetail?componentCode=C{lcsc_number}",
                headers=headers,
                timeout=10,
            )
        except requests.exceptions.RequestException as e:
            last_error = f"{type(e).__name__}: {e}"
            if attempt < retries:
                time.sleep(retry_delay * attempt)  # linear backoff
                continue
            return {"success": False, "msg": f"request failed after {retries} attempts ({last_error})"}

        if response.status_code != requests.codes.ok:
            last_error = f"non-OK HTTP response status ({response.status_code})"
            if attempt < retries:
                time.sleep(retry_delay * attempt)
                continue
            return {"success": False, "msg": last_error}

        try:
            data = response.json()
        except ValueError:
            last_error = "response was not valid JSON"
            if attempt < retries:
                time.sleep(retry_delay * attempt)
                continue
            return {"success": False, "msg": last_error}

        if not data.get("data"):
            return {
                "success": False,
                "msg": "returned JSON data does not have expected 'data' attribute",
            }

        if data["data"]["componentCode"] != f"C{lcsc_number}":
            return {
                "success": False,
                "msg": "returned missing or incorrect componentCode",
            }

        return {"success": True, "data": data}

    return {"success": False, "msg": f"request failed after {retries} attempts ({last_error})"}


def get_part_data_and_update_csv(lcsc_number, rows):
    response = get_part_data(lcsc_number)

    if not response["success"]:
        print(f"Failed to retrieve data: {response['msg']}")
        return rows

    part_details = response["data"]["data"]
    part_details["leastNumber"] = part_details["leastNumber"] or 0

    found = False
    for index, row in enumerate(rows):
        if len(row) > 0 and row[0] == str(lcsc_number):
            rows[index] = [
                lcsc_number,
                part_details["assemblyMode"],
                part_details["assemblyModeBatch"],
                part_details["assemblyProcess"],
                part_details["leastNumber"],
                part_details["lossNumber"],
                part_details["specialComponentFee"],
                part_details["componentLibraryType"],
            ]
            found = True
            break

    if not found:
        rows.append(
            [
                lcsc_number,
                part_details["assemblyMode"],
                part_details["assemblyModeBatch"],
                part_details["assemblyProcess"],
                part_details["leastNumber"],
                part_details["lossNumber"],
                part_details["specialComponentFee"],
                part_details["componentLibraryType"],
            ]
        )
        print(f"Added {lcsc_number} to the csv file")
    else:
        print(f"Updated {lcsc_number} in the csv file")
    return rows


# Path to the CSV file containing the component list
file_location = os.path.join("scraped", "ComponentList.csv")

# Print the initial size of the component list file
print(f"ComponentList.csv: {os.path.getsize(file_location)/1024:.1f}KiB")

# Load existing components from the CSV file
with open(file_location, "r", newline="", encoding="utf-8") as f:
    reader = csv.DictReader(f)
    components = list(reader)

print(f"Loaded {len(components)} components from {file_location}")

empty_page = False
page = 1
total_unseen_components = 0

while empty_page == False and page < 64:
    request_json = {
        "currentPage": page,
        "pageSize": 100,
        "keyword": None,
        "componentLibraryType": "base",
        "preferredComponentFlag": True,
        "stockFlag": None,
        "stockSort": None,
        "firstSortName": None,
        "secondSortName": None,
        "componentBrand": None,
        "componentSpecification": None,
        "componentAttributes": [],
        "searchSource": "search",
    }

    response = requests.post(url, headers=headers, json=request_json)
    unseen_components = 0

    if response.status_code == 200:
        page_components = re.findall(r'"componentCode":"C(\d+)"', response.text)
        for component in page_components:
            lcsc_code = component
            if update_component(components, lcsc_code):
                unseen_components += 1
                total_unseen_components += 1
    else:
        print(f"Failed to fetch data for page {page}. Status code: {response.status_code} Headers: {response.headers}")

    print(
        f"Page {page}: {response.status_code} Found {unseen_components} new components, {len(page_components)} previously seen components"
    )

    if len(page_components) < 1:
        empty_page = True
    page += 1

    time.sleep(3)  # Pause (try to not get rate limited)

print(f"Added {total_unseen_components} new components, current total components {len(components)}")

with open(file_location, "w", newline="") as f:
    writer = csv.DictWriter(f, fieldnames=components[0].keys())
    writer.writeheader()
    writer.writerows(components)

# Print the final size of the component list file
print(f"ComponentList.csv: {os.path.getsize(file_location)/1024:.1f}KiB")


list_file_location = os.path.join("scraped", "ComponentList.csv")
assembly_file_location = os.path.join("scraped", "assembly-details.csv")

with open(list_file_location, "r", newline="") as file:
    reader = csv.reader(file)
    components = [row[0] for row in reader][1:]

with open(assembly_file_location, "r", newline="") as file:
    reader = csv.reader(file)
    assembly_components = [row[0] for row in reader][1:]

try:
    with open(assembly_file_location, "r", newline="") as read_file:
        pass
except FileNotFoundError:
    with open(assembly_file_location, "w", newline="") as write_file:
        writer = csv.writer(write_file)
        writer.writerow(
            [
                "lcsc",
                "Assembly Type",
                "Assembly Type Batch",
                "Assembly Process",
                "Min Order Qty",
                "Attrition Qty",
                "Special Component Fee",
                "Component Library Type",
            ]
        )

with open(assembly_file_location, "r", newline="") as read_file:
    reader = csv.reader(read_file)
    rows = [row for row in reader]

# Check for parts not in ComponentList.csv
for lcsc_number in components:
    if lcsc_number not in assembly_components:
        rows = get_part_data_and_update_csv(int(lcsc_number), rows)
        time.sleep(0.4)  # Small gap between per-part requests to avoid tripping rate limiting

# Randomly check components already in the list
random_components = random.sample([c for c in components if c != ""], 400)
for lcsc_number in random_components:
    rows = get_part_data_and_update_csv(int(lcsc_number), rows)
    time.sleep(0.4)


with open(assembly_file_location, "w", newline="") as write_file:
    writer = csv.writer(write_file)
    writer.writerows(rows)
