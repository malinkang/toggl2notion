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

### 2. Optimized Sync Strategy (`insert_to_notion`)
The sync process is now split into two intelligent phases to maximize performance and minimize API usage:

#### Phase A: Incremental Forward Sync (Standard)
- **Range**: `Latest Entry in Notion (minus 24h)` -> `Now`
- **Purpose**: Captures new tasks and modifications to recent tasks.
- **Frequency**: Runs every time.

#### Phase B: Historical Backfill (Gap Fill)
- **Trigger**: Only runs if a significant gap (> 7 days) is detected between your **Notion Earliest Entry** and **Toggl Account Creation Date**.
- **Range**: `Account Creation Date` -> `Notion Earliest Entry`
- **Optimization**: **Does NOT re-sync** the data you already have in the middle. It specifically targets the missing historical chunk.
- **API Handling**: Automatically switches to Reports API and handles Free Tier limits (approx 1 year history) gracefully by stopping if a 402 error is encountered.

## How to Verify
1.  **Deployment**: Push the latest code to the repository.
2.  **Trigger**: Run the n8n workflow.
3.  **Observation**:
    - **Incremental**: You should see `🔄 Starting Incremental Sync from: ...`
    - **Backfill**: If you have missing history, you will see `🚀 Triggering GAP BACKFILL`.
    - **Efficiency**: The script will NOT waste time updating thousands of existing records in the middle of your timeline.

## Notes
- **Free Tier Limit**: Toggl's Reports API restricts free users to approximately 1 year of historical data. The script uses a "best effort" approach: it will sync as far back as allowed and stop gracefully if blocked by Toggl (Log: `🛑 Payment Required`).
