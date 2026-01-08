# VERIFICATION PLAN: Pick Token Round Selection Fix

## Overview
This fix ensures that pick tokens are always created for the **correct active round in the highest cycle**, preventing tokens from pointing to stale rounds after a rollover.

---

## A) ROOT CAUSE SUMMARY

### Problem Location
**File:** `lms_automation/app.py`

### Key Functions Affected
1. `send_picks()` (line 852) - Sends pick links to players
2. `get_due_reminders()` (line 3263) - Auto-generates reminder tokens
3. `admin_dashboard()` (line 644) - Shows current round info
4. `current_round_picks_status()` (line 652) - Pick submission tracking
5. `player_dashboard()` (line 3028) - Player view of current round
6. `get_player_upcoming_fixtures()` (line 3117) - Player fixture data
7. `reminders_dashboard()` (line 3361) - Admin reminder management
8. Round activation logic (line 1431) - Deactivates old rounds

### The Bug
All these functions used:
```python
Round.query.filter_by(status='active').first()
```

This returns the **first** active round by primary key ID, which could be from an **old cycle** after rollover.

**Example scenario:**
1. Cycle 1 ends, rollover happens
2. Round 3 from Cycle 1 is still marked `status='active'` (not deactivated)
3. Admin creates Round 1 for Cycle 2, marks it `status='active'`
4. Database now has TWO active rounds: R3/C1 and R1/C2
5. `send_picks()` calls `.first()` → returns R3/C1 (lower ID)
6. Pick tokens created with `round_id` pointing to R3/C1
7. Players see team history from Cycle 1 (wrong!)

---

## B) THE FIX

### 1. New Helper Function: `get_current_active_round()`
**Location:** `app.py` lines 108-146

**Logic:**
```python
def get_current_active_round():
    # Find all active rounds, ordered by cycle DESC
    active_rounds = Round.query.filter_by(status='active')\
        .order_by(Round.cycle_number.desc(), Round.id.desc()).all()

    if len(active_rounds) == 0:
        return None

    if len(active_rounds) == 1:
        return active_rounds[0]

    # Multiple active rounds detected!
    current_round = active_rounds[0]  # Highest cycle

    # Auto-deactivate stale rounds from older cycles
    for old_round in active_rounds[1:]:
        if old_round.cycle_number < current_round.cycle_number:
            old_round.status = 'completed'

    db.session.commit()
    return current_round
```

**Guarantees:**
- Always returns the active round from the **highest cycle**
- Auto-cleans stale rounds from old cycles
- Logs warnings for visibility

### 2. Updated 8 Critical Functions
All now use `get_current_active_round()` instead of raw `.filter_by(status='active').first()`

### 3. Stronger Round Activation Logic
**Location:** `app.py` lines 1431-1440

When admin activates a round:
```python
# OLD: Only deactivated ONE other active round
current_active = Round.query.filter_by(status='active').first()
if current_active:
    current_active.status = 'completed'

# NEW: Deactivates ALL other active rounds
other_active_rounds = Round.query.filter(
    Round.status == 'active',
    Round.id != round_id
).all()
for old_round in other_active_rounds:
    old_round.status = 'completed'
```

---

## C) VERIFICATION STEPS

### Step 1: Deploy to Railway
```bash
git add lms_automation/app.py
git commit -m "Fix pick token round selection to be cycle-aware"
git push
```

Railway will auto-deploy within 2-3 minutes.

---

### Step 2: Check Railway Logs (CRITICAL)

**What to look for immediately after deployment:**

#### Expected on first request:
```
[WARNING] MULTIPLE ACTIVE ROUNDS DETECTED: 2 active rounds found. Selecting Round 1 from Cycle 2
[WARNING] Auto-deactivating stale Round 3 from Cycle 1 (older than current Cycle 2)
```

This means the fix detected and cleaned up the bad state.

#### Expected on send_picks():
```
[INFO] Sending picks for Round 1, Cycle 2
```

#### Expected if only one active round:
```
(no warnings - clean state)
```

---

### Step 3: Database Verification (psql)

#### Connect to Railway DB:
```bash
# Get connection string from Railway dashboard
psql "postgresql://postgres:PASSWORD@HOST:PORT/railway"
```

#### Query 1: Check for multiple active rounds
```sql
SELECT id, round_number, cycle_number, status
FROM rounds
WHERE status = 'active'
ORDER BY cycle_number DESC, id DESC;
```

**Expected result:**
- 0 or 1 row only
- If 1 row: Should be from the highest cycle

**If > 1 row:** Run the cleanup script `fix_stale_active_rounds.sql`

---

#### Query 2: Verify pick tokens point to correct round
```sql
SELECT
    pt.id,
    pt.token,
    p.name AS player_name,
    r.round_number,
    r.cycle_number,
    r.status AS round_status,
    pt.created_at
FROM pick_tokens pt
JOIN players p ON pt.player_id = p.id
JOIN rounds r ON pt.round_id = r.id
WHERE pt.created_at > NOW() - INTERVAL '7 days'
ORDER BY pt.created_at DESC
LIMIT 10;
```

**Expected result:**
- All tokens created in last 7 days should point to rounds in the **highest cycle**
- `round_status` should be `'active'` or `'pending'`

**Red flag:**
- Token points to `cycle_number = 1` when current cycle is 2
- Token points to `status = 'completed'` round

---

#### Query 3: Check team history query behavior
```sql
-- Simulate team history lookup for a player in current cycle
WITH current_cycle AS (
    SELECT MAX(cycle_number) AS cycle FROM rounds WHERE status = 'active'
)
SELECT
    pk.id,
    p.name,
    r.round_number,
    r.cycle_number,
    pk.team_picked,
    pk.timestamp
FROM picks pk
JOIN players p ON pk.player_id = p.id
JOIN rounds r ON pk.round_id = r.id
CROSS JOIN current_cycle cc
WHERE p.id = 1  -- Replace with actual player ID
  AND r.cycle_number = cc.cycle
ORDER BY r.round_number;
```

**Expected result:**
- Only shows picks from the **current cycle**
- Does NOT mix picks from previous cycles

---

### Step 4: Functional Testing (CRITICAL)

#### Test A: Send Pick Links
1. Go to `/send_picks` (admin route)
2. Check Railway logs for:
   ```
   [INFO] Sending picks for Round X, Cycle Y
   ```
3. Open one of the WhatsApp links
4. Click the pick link
5. **Verify on pick form:**
   - Round number matches current round
   - Team history shows **only** current cycle picks
   - No teams from previous cycle marked as "used"

#### Test B: Player Dashboard
1. Go to `/dashboard/<token>` using a player token
2. **Verify:**
   - Current round displayed is from highest cycle
   - Fixtures shown match current round
   - League table shows current cycle data

#### Test C: Activate New Round After Fix
1. Admin creates a new round (Cycle 2, Round 2)
2. Admin activates it via API: `PUT /api/rounds/<id>` with `{"status": "active"}`
3. **Check Railway logs:**
   ```
   [WARNING] Auto-deactivating Round 1 (Cycle 2) when activating Round 2 (Cycle 2)
   ```
4. Run psql query:
   ```sql
   SELECT COUNT(*) FROM rounds WHERE status = 'active';
   ```
   **Expected:** Exactly 1 row

---

### Step 5: Rollover Scenario Test

**Simulate a full rollover:**

1. **Setup:** Ensure Cycle 2 Round 1 is active, all players have picked
2. **Mark all players eliminated:** Admin manually eliminates everyone
3. **Complete the round:** Admin marks Round 1 as `completed`
4. **Check rollover happens:** Logs should show:
   ```
   [INFO] ROLLOVER DETECTED: All X players lost in round 1
   [INFO] ROLLOVER HANDLED: Reactivated X players for Round 1 of Cycle 3
   ```
5. **Create new round:** Admin creates Round 1 for Cycle 3
6. **Activate it:** Admin marks it `active`
7. **Send picks:** Go to `/send_picks`
8. **Verify:**
   - Logs show `[INFO] Sending picks for Round 1, Cycle 3`
   - Pick tokens point to Cycle 3 Round 1
   - Team history is empty (new cycle)

---

## D) LOG SIGNATURES TO MONITOR

### ✅ GOOD (Expected after fix)
```
[INFO] Sending picks for Round 1, Cycle 2
```

### ⚠️ WARNING (Auto-cleanup triggered)
```
[WARNING] MULTIPLE ACTIVE ROUNDS DETECTED: 2 active rounds found. Selecting Round 1 from Cycle 2
[WARNING] Auto-deactivating stale Round 3 from Cycle 1 (older than current Cycle 2)
[WARNING] Auto-deactivating Round 1 (Cycle 1) when activating Round 2 (Cycle 2)
```

### ❌ BAD (Should NOT see after fix)
```
Player X in Cycle 1: ... Used teams: ...
```
(when current cycle is 2+)

---

## E) ROLLBACK PLAN (If needed)

If the fix causes issues:

```bash
git revert HEAD
git push
```

Railway will auto-deploy the previous version.

**Then:**
1. Check Railway logs for the exact error
2. Run `fix_stale_active_rounds.sql` manually to clean DB
3. Report the error for analysis

---

## F) SUCCESS CRITERIA

✅ **Fix is working correctly if:**

1. Railway logs show no `MULTIPLE ACTIVE ROUNDS DETECTED` warnings (or only once on first deploy)
2. `SELECT COUNT(*) FROM rounds WHERE status = 'active'` returns 0 or 1
3. All pick tokens created after deployment point to the highest cycle
4. Team history on pick forms shows ONLY current cycle picks
5. No player reports seeing "wrong teams" or "teams I didn't pick"
6. After rollover, new cycle tokens are created correctly

---

## G) ONGOING MONITORING

**Weekly health check query:**

```sql
-- Run this every week to ensure no drift
WITH active_summary AS (
    SELECT
        status,
        cycle_number,
        COUNT(*) AS count
    FROM rounds
    WHERE status = 'active'
    GROUP BY status, cycle_number
)
SELECT
    CASE
        WHEN (SELECT SUM(count) FROM active_summary) = 0 THEN '⚠️ No active rounds'
        WHEN (SELECT SUM(count) FROM active_summary) = 1 THEN '✅ Exactly 1 active round'
        WHEN (SELECT SUM(count) FROM active_summary) > 1 THEN '❌ MULTIPLE ACTIVE ROUNDS!'
        ELSE '❓ Unknown state'
    END AS health_status,
    COALESCE((SELECT MAX(cycle_number) FROM active_summary), 0) AS current_cycle,
    COALESCE((SELECT SUM(count) FROM active_summary), 0) AS active_count;
```

**Expected:** `✅ Exactly 1 active round` or `⚠️ No active rounds` (between games)

---

## H) CLEANUP SCRIPT (Optional)

If you want to manually clean up bad state **before** the app auto-fixes:

```bash
psql "postgresql://..." < fix_stale_active_rounds.sql
```

This script:
1. Shows current active rounds
2. Identifies stale ones
3. (Optionally) deactivates them
4. Verifies pick token health

---

## I) ONE QUESTION

Do you want me to:
1. **Commit and push now** (recommended - surgical fix, low risk)
2. **Review the diff one more time** before pushing
3. **Test locally first** (if you have local DB setup)

The fix is minimal, well-scoped, and includes auto-cleanup + logging for visibility.
