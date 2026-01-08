-- =====================================================
-- DB CLEANUP: Fix Stale Active Rounds After Rollover
-- =====================================================
--
-- PURPOSE: Identify and deactivate old-cycle rounds that are
--          still marked 'active' after a rollover event.
--
-- WHEN TO RUN: After deploying the get_current_active_round() fix
--              AND if you suspect multiple active rounds exist
--
-- SAFETY: Run SELECT queries first to verify before UPDATE
-- =====================================================

-- STEP 1: Check current state
-- ===========================
SELECT
    id,
    round_number,
    cycle_number,
    status,
    pl_matchday,
    first_kickoff_at
FROM rounds
WHERE status = 'active'
ORDER BY cycle_number DESC, id DESC;

-- Expected: Should see 0 or 1 active round
-- If > 1: The new code will auto-fix on next request, but you can manually fix now

-- STEP 2: Identify the CORRECT active round (highest cycle)
-- ==========================================================
WITH max_cycle AS (
    SELECT MAX(cycle_number) AS current_cycle
    FROM rounds
    WHERE status = 'active'
)
SELECT
    r.id,
    r.round_number,
    r.cycle_number,
    r.status,
    CASE
        WHEN r.cycle_number = mc.current_cycle THEN '✓ CURRENT'
        ELSE '✗ STALE (should be completed)'
    END AS action_needed
FROM rounds r
CROSS JOIN max_cycle mc
WHERE r.status = 'active'
ORDER BY r.cycle_number DESC, r.id DESC;

-- STEP 3 (OPTIONAL): Manually deactivate stale rounds
-- ====================================================
-- ONLY RUN THIS IF YOU WANT TO CLEAN UP IMMEDIATELY
-- OTHERWISE, the app will auto-clean on next get_current_active_round() call
--
-- UNCOMMENT TO EXECUTE:
/*
UPDATE rounds
SET status = 'completed'
WHERE status = 'active'
  AND cycle_number < (
      SELECT MAX(cycle_number)
      FROM rounds
      WHERE status = 'active'
  );
*/

-- STEP 4: Verify pick tokens point to correct rounds
-- ===================================================
SELECT
    pt.id AS token_id,
    pt.token,
    p.name AS player_name,
    r.round_number,
    r.cycle_number,
    r.status AS round_status,
    pt.created_at,
    CASE
        WHEN r.status != 'active' THEN '⚠ WARNING: Token points to non-active round'
        WHEN r.cycle_number != (SELECT MAX(cycle_number) FROM rounds WHERE status = 'active') THEN '⚠ WARNING: Token points to old cycle'
        ELSE '✓ OK'
    END AS validation
FROM pick_tokens pt
JOIN players p ON pt.player_id = p.id
JOIN rounds r ON pt.round_id = r.id
WHERE pt.created_at > NOW() - INTERVAL '7 days'
ORDER BY pt.created_at DESC
LIMIT 20;

-- STEP 5: Check for picks created against wrong rounds
-- =====================================================
SELECT
    pk.id AS pick_id,
    p.name AS player_name,
    r.round_number,
    r.cycle_number,
    r.status,
    pk.team_picked,
    pk.timestamp,
    CASE
        WHEN r.cycle_number != (SELECT MAX(cycle_number) FROM rounds) THEN '⚠ Pick in old cycle'
        ELSE '✓ OK'
    END AS validation
FROM picks pk
JOIN players p ON pk.player_id = p.id
JOIN rounds r ON pk.round_id = r.id
WHERE pk.timestamp > NOW() - INTERVAL '7 days'
ORDER BY pk.timestamp DESC
LIMIT 20;

-- STEP 6: Summary health check
-- =============================
SELECT
    'Total Rounds' AS metric,
    COUNT(*)::text AS value
FROM rounds
UNION ALL
SELECT
    'Active Rounds' AS metric,
    COUNT(*)::text AS value
FROM rounds
WHERE status = 'active'
UNION ALL
SELECT
    'Max Cycle Number' AS metric,
    COALESCE(MAX(cycle_number)::text, 'None') AS value
FROM rounds
UNION ALL
SELECT
    'Cycles with Active Rounds' AS metric,
    COUNT(DISTINCT cycle_number)::text AS value
FROM rounds
WHERE status = 'active'
UNION ALL
SELECT
    'Recent Pick Tokens (7d)' AS metric,
    COUNT(*)::text AS value
FROM pick_tokens
WHERE created_at > NOW() - INTERVAL '7 days';

-- Expected results:
-- - Active Rounds: 0 or 1
-- - Cycles with Active Rounds: 0 or 1
-- - If > 1: The app will auto-fix, or run STEP 3 manually
