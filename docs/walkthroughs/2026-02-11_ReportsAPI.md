# Walkthrough: Implementing Full Historical Data Sync (Reports API)

## Overview
This update enables the synchronization of Toggl time entries older than 90 days, bypassing the limitations of the free plan's Time Entries API. It uses the **Detailed Reports API** to fetch historical data year-by-year.

## Key Changes

### 1. Reports API Integration (`toggl.py`)
- **New Function**: `get_detailed_report(workspace_id, start_date, end_date)`
- **Capabilities**:
    - Fetches data for ranges > 90 days.
    - Handles pagination (50 items per page).
    - Implements rate limiting (1.1s delay between requests).
    - Transforms report data structure to match the existing Time Entry format.

### 2. Smart Backfill Logic (`insert_to_notion`)
- **Gap Detection**:
    - Compares the `earliest` entry in Notion with the Toggl account's `created_at` date.
    - If a gap of > 7 days is detected, the script automatically triggers a **Full Backfill** mode.
- **Workflow**:
    - **Standard Mode**: Syncs incrementally from the latest Notion entry (minus 24h).
    - **Backfill Mode**: Syncs from `created_at` date up to `now`.
    - **Hybrid API Usage**:
        - Recent data (< 90 days) uses the fast `Time Entries API`.
        - Historical data (> 90 days) automatically switches to `Reports API`.

## How to Verify
1.  **Deployment**: Push the latest code to the repository.
2.  **Trigger**: Run the n8n workflow or wait for the scheduled trigger.
3.  **Observation**:
    - Check the execution logs.
    - If your Notion database was incomplete, you should see:
      `⚠️ Missing history detected! ... Triggering FULL BACKFILL`
    - You will see logs indicating "Switching to Reports API" for older dates.
    - Verify that entries from previous years (e.g., 2021, 2022) are appearing in Notion.

## Notes
- **Performance**: A full backfill might take time (e.g., 10-20 minutes for 5 years of data) due to API rate limits. This only happens once.
- **Safety**: The logic checks for existing pages in Notion before creating new ones, preventing duplicates.
