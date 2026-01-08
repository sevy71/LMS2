# PICK TOKEN ROUND SELECTION FIX - SUMMARY

## THE BUG
Pick tokens were sometimes created for the **wrong round/cycle** after rollover, causing:
- Team history showing picks from previous cycle
- Players seeing incorrect "teams used" list
- Pick links pointing to stale rounds

**Root cause:** All code used `Round.query.filter_by(status='active').first()` which returns the **first** active round by ID, not necessarily the current cycle's round.

---

## THE FIX

### 1. New Helper Function: `get_current_active_round()`
**File:** `app.py` lines 108-146

- Returns active round from **highest cycle** (not just first by ID)
- Auto-deactivates stale rounds from old cycles
- Logs warnings for visibility

### 2. Updated 8 Critical Functions
All now use `get_current_active_round()`:
- `send_picks()` - Pick link generation ✅
- `get_due_reminders()` - Reminder tokens ✅
- `admin_dashboard()` - Admin UI ✅
- `current_round_picks_status()` - Pick tracking ✅
- `player_dashboard()` - Player view ✅
- `get_player_upcoming_fixtures()` - Player fixtures ✅
- `reminders_dashboard()` - Reminder UI ✅
- Round activation logic - Deactivates ALL other active rounds ✅

### 3. Stronger Activation Logic
When activating a round, now deactivates **ALL** other active rounds (not just one).

---

## FILES CHANGED
- ✅ `lms_automation/app.py` - 1 new function, 8 function updates, 1 logic enhancement
- ✅ `fix_stale_active_rounds.sql` - DB cleanup script (optional)
- ✅ `VERIFICATION_PLAN.md` - Detailed verification steps

**Total lines changed:** ~60 lines (surgical, minimal)

---

## GUARANTEES AFTER FIX

1. ✅ At most ONE active round exists at any time
2. ✅ Active round is always from **highest cycle**
3. ✅ Pick tokens created for **correct cycle**
4. ✅ Team history shows **only current cycle** picks
5. ✅ Stale rounds auto-cleaned when detected
6. ✅ All operations logged for visibility

---

## QUICK VERIFICATION (After Deploy)

### Railway Logs - Look for:
```
[INFO] Sending picks for Round X, Cycle Y
```
(Cycle should match current cycle)

### psql Query:
```sql
SELECT COUNT(*) FROM rounds WHERE status = 'active';
```
**Expected:** 0 or 1

### Functional Test:
1. Go to `/send_picks`
2. Open a pick link
3. Verify team history shows **only** current cycle picks

---

## DB CLEANUP (If Needed)

Run once after deployment to clean existing bad state:
```bash
psql "postgresql://..." < fix_stale_active_rounds.sql
```

Or let the app auto-clean on first request (it will log warnings).

---

## ROLLBACK (If Needed)

```bash
git revert HEAD
git push
```

---

## MONITORING

**Health check query (run weekly):**
```sql
SELECT COUNT(*) AS active_rounds FROM rounds WHERE status = 'active';
```
**Expected:** 0 or 1

**Pick token validation (last 7 days):**
```sql
SELECT
    r.cycle_number,
    COUNT(*) AS tokens_created
FROM pick_tokens pt
JOIN rounds r ON pt.round_id = r.id
WHERE pt.created_at > NOW() - INTERVAL '7 days'
GROUP BY r.cycle_number
ORDER BY r.cycle_number DESC;
```
**Expected:** All tokens in **highest cycle only**

---

## IMPACT

- ✅ No schema changes
- ✅ No data migrations
- ✅ No rule/logic changes
- ✅ Backward compatible
- ✅ Auto-healing (cleans bad state)
- ✅ Fully logged for debugging

**Risk level:** LOW (surgical fix, defensive auto-cleanup)
