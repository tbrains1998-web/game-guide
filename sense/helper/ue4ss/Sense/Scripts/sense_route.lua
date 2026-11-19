-- sense_route.lua - a walking route for the quest path (the game's own walking map / navmesh), UE4SS.
--
--   M.update(pawn, playerCm, targetCm, nowS, modDir) -> route | nil
--       pawn      this tick's valid player pawn (used as the world context and the path finding context)
--       playerCm  {X=,Y=,Z=} player location in UE cm      targetCm  {X=,Y=,Z=} target location in UE cm
--       nowS      seconds (os.clock() of this tick)          modDir    the Sense mod folder (...\Mods\Sense)
--     route = { pts = {{x,y,z}, ...} in METRES (UE cm / 100), src = "nav" | "nav_partial" | "ground" }
--       nav          a full path over the walking map (corner points)
--       nav_partial  the walking map path only goes part of the way (the goal is not reachable / not loaded)
--       ground       no path: points of the straight line, each one dropped onto the walking map floor
--   M.setLog(fn)    log function (main.lua's log); the text handed to it starts with "route: ". In UE4SS.log the
--                   real lines read "[Sense] <seconds> route: ..." (main.lua's log adds the time), so search for
--                   " route: ". Without setLog (tests) the line is "[Sense] route: ...".
--   M.reset()       forget everything (tests)
--   M._state()      the session memory (tests)
--
-- Safety (crash history, see main.lua):
-- * The caller runs update() inside its ExecuteInGameThread sample with the in-flight guard.
-- * Kill switch: the file <modDir>\route_off.txt (made by Game Guide's "Quest path follows the ground" switch).
--   While it exists update() makes NO game calls at all. The file is looked at once per 2 s and again right before
--   every round of game calls (so at most about 1.5 file checks per second, never a game call while it exists).
-- * Real game calls at most once per 1 s, and only when the target changed, the player moved more than 2 m, or 3 s
--   passed; every other tick gets the last route back (pure Lua, no game call). After a map/world change (another
--   pawn) nothing is asked for 5 s. Each round is timed: 2 slow rounds (> 8 ms) in a row -> one round per 5 s
--   (back to normal after 3 fast rounds). 3 rounds in a row with an error -> off for this session (logged once).
-- * Every game touch is inside pcall. Only two UFunctions on the NavigationSystemV1 class default object
--   (static functions, called the way UE4SS calls static blueprint functions):
--     K2_ProjectPointToNavigation(WorldContext, Point, out ProjectedLocation, NavData, FilterClass, QueryExtent)
--       = exactly 6 Lua arguments; the out table is filled with X/Y/Z; the first result is the bool.
--     FindPathToLocationSynchronously(WorldContext, PathStart, PathEnd, PathfindingContext, FilterClass)
--       = exactly 5 Lua arguments; returns a NavigationPath object.
-- * NavigationPath.PathPoints (TArray<FVector>) is the ONLY array this helper ever reads (a deliberate
--   exception). Rules: n = arr:GetArrayNum() checked >= 2 just before, cap 64, numeric loop 1..n,
--   X/Y/Z read at once. Never ipairs/pairs on it (UE4SS grows the array at Num+1 -> endless loop), never index 0
--   (raises) or past Num (UE4SS ADDS zeroed elements to the game's array), never ForEach / get / Get, never keep the
--   path, the array or an element wrapper after the call (the path object is not rooted: the next garbage
--   collection frees it). path:IsValid() is not trusted to mean "a good path" (UNavigationPath has its own IsValid
--   UFunction with the same name): wrapper valid AND n >= 2 is what counts.

local M = {}

-- ---------- settings ----------
local NAV_CDO_PATH  = "/Script/NavigationSystem.Default__NavigationSystemV1"
local CALL_MIN_S    = 1.0      -- real game calls at most this often
local BACKOFF_S     = 5.0      -- ... or this often after slow rounds
local REFRESH_S     = 3.0      -- ask again after this long even when nothing changed
local MOVE_M        = 2.0      -- ask again when the player moved more than this
local TARGET_MOVE_M = 1.0      -- the target moved more than this: a new round is due (at most one per CALL_MIN_S)
local TARGET_DROP_M = 10.0     -- the target moved more than this: the old route is not sent any more (another target).
                               -- A target that walks (NPC) keeps its last route between rounds instead of
                               -- flickering nav -> straight line -> nav once a second.
local DROP_MOVE_M   = 20.0     -- the player moved this far since the route was made: the old route is not sent
local WORLD_WAIT_S  = 5.0      -- after a map/world change (another pawn) wait this long
local KILL_CHECK_S  = 2.0      -- route_off.txt is looked at this often
local SLOW_MS       = 8.0      -- a round slower than this is "slow"
local SLOW_N        = 2        -- this many slow rounds in a row -> back off
local FAST_N        = 3        -- this many fast rounds in a row -> normal again
local MAX_ERRS      = 3        -- this many rounds in a row with an error -> off for the session
local FAR_M         = 60.0     -- a target farther than this (flat) gets a mid-goal in its direction
local MID_GOALS_M   = { 55.0, 40.0, 25.0 }   -- mid-goals tried in this order (the walking map is only loaded ~100 m around)
local MAX_PTS       = 64       -- most path points read
local GROUND_STEP_M = 3.0      -- ground fallback: one point every 3 m ...
local GROUND_MAX_M  = 30.0     -- ... up to 30 m
local GROUND_MIN_GAIN_M = 0.5  -- ... and each kept point at least 0.5 m further along the line than the last
local MAX_POINT_M   = 2000.0   -- a path point farther than this from the player is junk
local EXTENT        = { 500.0, 500.0, 2000.0 }   -- K2_ProjectPointToNavigation QueryExtent (cm)

M.SETTINGS = { TARGET_MOVE_M = TARGET_MOVE_M, TARGET_DROP_M = TARGET_DROP_M, CALL_MIN_S = CALL_MIN_S, BACKOFF_S = BACKOFF_S, REFRESH_S = REFRESH_S, MOVE_M = MOVE_M,
               WORLD_WAIT_S = WORLD_WAIT_S, KILL_CHECK_S = KILL_CHECK_S, SLOW_MS = SLOW_MS, MAX_ERRS = MAX_ERRS,
               FAR_M = FAR_M, MAX_PTS = MAX_PTS, GROUND_STEP_M = GROUND_STEP_M, GROUND_MAX_M = GROUND_MAX_M,
               NAV_CDO_PATH = NAV_CDO_PATH }

-- ---------- log ----------
local LOG = nil
function M.setLog(fn) if type(fn) == "function" then LOG = fn end end
local function log(text)
    local line = "route: " .. tostring(text)
    if LOG and pcall(LOG, line) then return end
    pcall(print, "[Sense] " .. line .. "\n")
end

-- ---------- session memory ----------
local S = {}
local function fresh()
    S = {
        nav = nil,              -- cached NavigationSystemV1 class default object wrapper (checked each round)
        off = false,            -- switched off for this session (errors)
        errs = 0,               -- rounds in a row with an error
        slow = 0, fast = 0,     -- slow / fast rounds in a row
        backoff = false,        -- one round per BACKOFF_S
        killOn = false, killAt = -math.huge,   -- route_off.txt seen, when it was last looked at
        lastCall = -math.huge,  -- nowS of the last round with game calls
        callPlayer = nil,       -- player cm at the last round
        callTarget = nil,       -- target cm at the last round
        route = nil,            -- last route (pure Lua tables, safe to keep)
        routeTarget = nil,      -- target cm the route belongs to
        pawnKey = nil,          -- full name of the pawn of the last round
        waitUntil = -math.huge, -- world change: no game calls before this
        logged = {},            -- message key -> true
        okSrc = {},             -- src -> true once logged
        rounds = 0,             -- rounds with game calls (tests)
        lastMs = 0,             -- duration of the last round (tests / F3)
        lastWhy = nil,          -- why the last round gave no route (F3)
    }
end
fresh()
function M.reset() fresh() end
M._state = function() return S end

local function logOnce(key, text)
    if S.logged[key] then return end
    S.logged[key] = true
    log(text)
end

-- ---------- pure helpers ----------
local function goodNum(n) return type(n) == "number" and n == n and n ~= math.huge and n ~= -math.huge end

-- {X,Y,Z} (or x/y/z) plain Lua table -> {X=,Y=,Z=} copy, or nil. Only plain tables (the caller's own data).
local function cmOf(v)
    if type(v) ~= "table" then return nil end
    local x, y, z = rawget(v, "X"), rawget(v, "Y"), rawget(v, "Z")
    if x == nil and y == nil and z == nil then x, y, z = rawget(v, "x"), rawget(v, "y"), rawget(v, "z") end
    if goodNum(x) and goodNum(y) and goodNum(z) then return { X = x, Y = y, Z = z } end
    return nil
end

local function dist3(a, b)
    local dx, dy, dz = a.X - b.X, a.Y - b.Y, a.Z - b.Z
    return math.sqrt(dx * dx + dy * dy + dz * dz)
end

local function flat(a, b)
    local dx, dy = a.X - b.X, a.Y - b.Y
    return math.sqrt(dx * dx + dy * dy)
end

local function joinPath(dir, name)
    if type(dir) ~= "string" or dir == "" then return nil end
    local last = dir:sub(-1)
    if last == "\\" or last == "/" then return dir .. name end
    return dir .. "\\" .. name
end

local function fileExists(path)
    if not path then return false end
    local ok, res = pcall(function()
        local f = io.open(path, "rb")
        if f then f:close(); return true end
        return false
    end)
    return ok and res == true
end

-- ---------- game touches (each one in pcall) ----------
local function valid(obj)
    if obj == nil then return false end
    local ok, v = pcall(function() return obj:IsValid() end)
    return ok and v == true
end

local function fullName(obj)
    local ok, s = pcall(function() return obj:GetFullName() end)
    if ok and type(s) == "string" then return s end
    return nil
end

-- the NavigationSystemV1 class default object (never freed; still checked with IsValid every round)
local function navCdo(R)
    if S.nav ~= nil and valid(S.nav) then return S.nav end
    S.nav = nil
    local ok, obj = pcall(function()
        if type(StaticFindObject) ~= "function" then return nil end
        return StaticFindObject(NAV_CDO_PATH)
    end)
    if not ok then R.err = "StaticFindObject: " .. tostring(obj); return nil end
    if obj ~= nil and valid(obj) then S.nav = obj; return obj end
    return nil
end

local function extent() return { X = EXTENT[1], Y = EXTENT[2], Z = EXTENT[3] } end

-- the out table of a struct out param: UE4SS fills the same table with X/Y/Z (also accepted: a nested
-- ProjectedLocation table, or lower case). Plain Lua tables only.
local function outVec(out)
    local v = cmOf(out)
    if v then return v end
    if type(out) == "table" then
        local inner = rawget(out, "ProjectedLocation")
        if type(inner) == "table" then return cmOf(inner) end
    end
    return nil
end

-- point (cm) -> the nearest walking map floor point (cm), or nil. 6 Lua arguments exactly.
local function project(nav, pawn, pt, R)
    local out = {}
    local ok, hit = pcall(function()
        return nav:K2_ProjectPointToNavigation(pawn, { X = pt.X, Y = pt.Y, Z = pt.Z }, out, nil, nil, extent())
    end)
    R.calls = R.calls + 1
    if not ok then R.err = "K2_ProjectPointToNavigation: " .. tostring(hit); return nil end
    if hit ~= true then return nil end
    return outVec(out)
end

-- start -> goal (cm) over the walking map: list of {x,y,z} metres + partial, or nil + why. 5 Lua arguments exactly.
-- The path object, its PathPoints array and the element wrappers are only used inside this function.
local function findPath(nav, pawn, startCm, goalCm, R)
    local okc, path = pcall(function()
        return nav:FindPathToLocationSynchronously(pawn, { X = startCm.X, Y = startCm.Y, Z = startCm.Z },
            { X = goalCm.X, Y = goalCm.Y, Z = goalCm.Z }, pawn, nil)
    end)
    R.calls = R.calls + 1
    if not okc then R.err = "FindPathToLocationSynchronously: " .. tostring(path); return nil, "error" end
    if path == nil then return nil, "no path object" end
    -- "wrapper valid": a null object return is an invalid wrapper. IsValid() == true is required, but it is not
    -- trusted to mean the path is good (name clash with UNavigationPath::IsValid); n >= 2 decides.
    if not valid(path) then return nil, "no path object" end
    local pts, why, partial = nil, nil, false
    local okr, errr = pcall(function()
        local arr = path.PathPoints
        if arr == nil then why = "no PathPoints"; return end
        local okn, n = pcall(function() return arr:GetArrayNum() end)
        if not okn or type(n) ~= "number" or n ~= n then why = "no point count"; return end
        n = math.floor(n)
        if n < 2 then why = "empty path (" .. tostring(n) .. " points)"; return end
        if n > MAX_PTS then n = MAX_PTS end
        local list = {}
        for i = 1, n do                         -- numeric loop only; i is never 0 and never past Num
            local e = arr[i]
            if e == nil then why = "point " .. i .. " missing"; return end
            local x, y, z = e.X, e.Y, e.Z      -- read now; e is not kept
            if not (goodNum(x) and goodNum(y) and goodNum(z)) then why = "bad point " .. i; return end
            list[i] = { x / 100, y / 100, z / 100 }
        end
        pts = list
    end)
    if not okr then R.err = "PathPoints: " .. tostring(errr); return nil, "error" end
    if not pts then return nil, why or "no points" end
    local okp, p = pcall(function() return path:IsPartial() end)
    partial = okp and p == true
    return pts, partial
end

-- the goals to try (cm): the target itself when it is near, else mid-goals in its direction at the player's height
local function goals(playerCm, targetCm)
    local d = flat(playerCm, targetCm) / 100
    local list = {}
    if d <= FAR_M then list[#list + 1] = { X = targetCm.X, Y = targetCm.Y, Z = targetCm.Z } end
    if d > 0.01 then
        local ux, uy = (targetCm.X - playerCm.X) / (d * 100), (targetCm.Y - playerCm.Y) / (d * 100)
        for k = 1, #MID_GOALS_M do
            local m = MID_GOALS_M[k]
            if m < d then
                list[#list + 1] = { X = playerCm.X + ux * m * 100, Y = playerCm.Y + uy * m * 100, Z = playerCm.Z }
            end
        end
    end
    return list
end

-- a route point list is sane: >= 2 points, all near the player
local function sane(pts, playerCm)
    if type(pts) ~= "table" or #pts < 2 then return false end
    local px, py, pz = playerCm.X / 100, playerCm.Y / 100, playerCm.Z / 100
    for i = 1, #pts do
        local p = pts[i]
        local dx, dy, dz = p[1] - px, p[2] - py, p[3] - pz
        if math.sqrt(dx * dx + dy * dy + dz * dz) > MAX_POINT_M then return false end
    end
    return true
end

-- one round of game calls -> route | nil (R.err set when a call raised; R.why = why there is no route)
local function compute(pawn, playerCm, targetCm, R)
    local nav = navCdo(R)
    if not nav then
        if not R.err then logOnce("nocdo", "no NavigationSystemV1 found (" .. NAV_CDO_PATH .. "); trying again later") end
        R.why = "no navigation system"
        return nil
    end
    local list = goals(playerCm, targetCm)
    if #list == 0 then R.why = "target too close"; return nil end
    local snapped, aim = nil, list[1]
    for k = 1, #list do
        local g = list[k]
        snapped = project(nav, pawn, g, R)
        if R.err then R.why = "error"; return nil end
        if snapped then aim = g; break end
    end
    local startCm = project(nav, pawn, playerCm, R)
    if R.err then R.why = "error"; return nil end
    if snapped then
        local pts, partial = findPath(nav, pawn, startCm or playerCm, snapped, R)
        if R.err then R.why = "error"; return nil end
        if pts and sane(pts, playerCm) then
            return { pts = pts, src = (partial == true) and "nav_partial" or "nav" }
        end
        R.why = "no nav path (" .. tostring(partial) .. ")"
        aim = snapped
    else
        R.why = "goal not on the walking map"
    end
    -- ground fallback: the straight line toward the goal, each point dropped onto the walking map floor
    local d = flat(playerCm, aim) / 100
    if d < 0.01 then return nil end
    local len = math.min(GROUND_MAX_M, d)
    -- only points that move FORWARD along the line are kept: a sample inside a wall or building snaps to the
    -- nearest walking-map edge, which can be sideways or behind the last point (the dots would zigzag back)
    local ux, uy = (aim.X - playerCm.X) / (d * 100), (aim.Y - playerCm.Y) / (d * 100)
    local function along(c) return (c.X - playerCm.X) * ux + (c.Y - playerCm.Y) * uy end   -- cm along the line
    local pts, lastAlong = {}, 0
    if startCm then
        pts[1] = { startCm.X / 100, startCm.Y / 100, startCm.Z / 100 }
        lastAlong = math.max(0, along(startCm))
    end
    local s = GROUND_STEP_M
    while s <= len + 1e-6 do
        local f = s / d
        local q = { X = playerCm.X + (aim.X - playerCm.X) * f, Y = playerCm.Y + (aim.Y - playerCm.Y) * f,
                    Z = playerCm.Z + (aim.Z - playerCm.Z) * f }
        local hit = project(nav, pawn, q, R)
        if R.err then R.why = "error"; return nil end
        if hit then
            local a = along(hit)
            if a > lastAlong + GROUND_MIN_GAIN_M * 100 then
                pts[#pts + 1] = { hit.X / 100, hit.Y / 100, hit.Z / 100 }
                lastAlong = a
            end
        end
        s = s + GROUND_STEP_M
    end
    if sane(pts, playerCm) then return { pts = pts, src = "ground" } end
    R.why = (R.why or "") .. "; ground: " .. #pts .. " points"
    return nil
end

-- ---------- the entry point ----------
function M.update(pawn, playerCm, targetCm, nowS, modDir)
    local p, tg = cmOf(playerCm), cmOf(targetCm)
    if not p or not tg or not goodNum(nowS) then return nil end
    if S.off then return nil end

    -- kill switch (a file, no game call), looked at once per KILL_CHECK_S and again right before every round
    local function killCheck()
        S.killAt = nowS
        local on = fileExists(joinPath(modDir, "route_off.txt"))
        if on ~= S.killOn then
            S.killOn = on
            log(on and "route_off.txt found: walking route is OFF (no navigation calls)"
                   or "route_off.txt gone: walking route is ON")
            if on then S.route, S.routeTarget = nil, nil end
        end
    end
    if nowS - S.killAt >= KILL_CHECK_S or nowS < S.killAt then killCheck() end
    if S.killOn then return nil end

    -- another target (moved more than TARGET_DROP_M): the old route is not used any more. A smaller move only
    -- makes the next round due (below), the old route is kept until that round replaces it.
    if S.routeTarget and dist3(S.routeTarget, tg) > TARGET_DROP_M * 100 then S.route, S.routeTarget = nil, nil end
    -- the player went far since the route was made (teleport, new map): not sent until the next round
    if S.route and S.callPlayer and flat(S.callPlayer, p) > DROP_MOVE_M * 100 then S.route, S.routeTarget = nil, nil end

    -- throttle: pure Lua until a round is due
    if nowS < S.waitUntil then return nil end
    local interval = S.backoff and BACKOFF_S or CALL_MIN_S
    local since = nowS - S.lastCall
    if since >= 0 and since < interval then return S.route end
    local due = S.callTarget == nil or dist3(S.callTarget, tg) > TARGET_MOVE_M * 100
        or S.callPlayer == nil or dist3(S.callPlayer, p) > MOVE_M * 100
        or since >= REFRESH_S or since < 0
    if not due then return S.route end

    -- a round with game calls (the switch file is looked at once more first, so it works at once)
    if S.killAt ~= nowS then
        killCheck()
        if S.killOn then return nil end
    end
    S.lastCall = nowS
    if not valid(pawn) then return S.route end
    local key = fullName(pawn) or "?"
    if key ~= S.pawnKey then
        local first = S.pawnKey == nil
        S.pawnKey = key
        S.route, S.routeTarget, S.callPlayer, S.callTarget = nil, nil, nil, nil
        S.waitUntil = nowS + WORLD_WAIT_S
        log((first and "first player seen" or "world changed (another player object)") ..
            string.format(": waiting %.0f s before the first walking route", WORLD_WAIT_S))
        return nil
    end
    S.callPlayer, S.callTarget = p, tg
    local R = { calls = 0, err = nil, why = nil }
    local t0 = os.clock()
    local okc, route = pcall(compute, pawn, p, tg, R)
    local ms = (os.clock() - t0) * 1000
    S.rounds, S.lastMs = S.rounds + 1, ms
    if not okc then R.err = "internal: " .. tostring(route); route = nil end

    -- errors: 3 rounds in a row -> off for the session
    if R.err then
        S.errs = S.errs + 1
        logOnce("err:" .. S.errs, "error (" .. S.errs .. " in a row): " .. tostring(R.err))
        if S.errs >= MAX_ERRS then
            S.off, S.route, S.routeTarget = true, nil, nil
            log("switched off for this session after " .. MAX_ERRS .. " errors in a row (the quest path uses the straight line)")
            return nil
        end
    else
        S.errs = 0
    end

    -- slow rounds -> back off
    if goodNum(ms) and ms > SLOW_MS then
        S.slow, S.fast = S.slow + 1, 0
        if S.slow >= SLOW_N and not S.backoff then
            S.backoff = true
            log(string.format("slow (%.1f ms, %d rounds in a row over %.0f ms): one round per %.0f s now",
                ms, S.slow, SLOW_MS, BACKOFF_S))
        end
    else
        S.fast, S.slow = S.fast + 1, 0
        if S.backoff and S.fast >= FAST_N then
            S.backoff = false
            log(string.format("fast again (%.1f ms): one round per %.0f s", ms, CALL_MIN_S))
        end
    end

    if type(route) == "table" then
        S.route, S.routeTarget, S.lastWhy = route, tg, nil
        if not S.okSrc[route.src] then
            S.okSrc[route.src] = true
            log(string.format("first %s route: %d points, %d calls, %.2f ms", route.src, #route.pts, R.calls, ms))
        end
        return route
    end
    S.route, S.routeTarget, S.lastWhy = nil, nil, R.why
    if not R.err then
        local why = tostring(R.why)
        logOnce("why:" .. (why:match("^[%a ]+") or why), "no route yet: " .. why)
    end
    return nil
end

return M
