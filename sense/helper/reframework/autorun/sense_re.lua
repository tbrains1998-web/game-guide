-- sense_re.lua  -  Game Guide "Sense" helper for RE Engine games (REFramework), loader.
-- Read-only: it never changes the game. It writes only into <game>\reframework\data (sense\sense.json, sense_re\*).
-- Files: autorun\sense_re.lua (this) + autorun\sense_re\{core,profile,world,scan,learn,target,out}.lua
--        data\sense_re\profiles\_default.json + <game>.json   (game key = data\sense_re\game.txt written by the
--        installer, else reframework:get_game_name(); e.g. re9, dd2 - see profile.game_key)
--
-- Module contract (each module returns a table):
--   M.init(ctx)  optional, called once after every module loaded (in the order below)
--   M.tick(ctx)  called by the ONE re.on_application_entry("LateUpdateBehavior") callback, 10 times a second,
--                in this order: world, scan, learn, target, out.
-- ctx (shared table, plain Lua data only - never keep game objects or addresses in it across ticks):
--   ctx.game     "re9" | "dd2" | ...         ctx.profile  merged profile (profile.lua)
--   ctx.now      seconds (core.clock)        ctx.dt       seconds since the previous tick     ctx.seq  tick count
--   ctx.log(msg) log one line                ctx.mods     {core=, profile=, world=, scan=, learn=, target=, out=}
--   ctx.version  "reframework-sense/0.1"
--   filled by the modules each tick (names shared by all modules):
--   world  -> ctx.player = {x,y,z} (metres, Y up) or nil, ctx.cam, ctx.screen, ctx.area ("area<n>": goes up when
--             the player comes back after > 1 s without one; scan treats a new value as an area change), ...
--   learn  -> ctx.door_events = list of {id, pos={x,y,z}, t=ctx.now at unlock, kind?, label?, cls?}
--   target -> ctx.target = nil | {pos={x,y,z}, has_z=true, kind, id, label, src}
--   out    -> writes sense/sense.json
-- Safety: every module call is in pcall; errors feed core's kill switch (20 errors in 1 s -> 10 s pause).
-- If any module fails to load, the helper logs why and registers NO callback (it does nothing).

local VERSION = "reframework-sense/0.1"
local ORDER = { "world", "scan", "learn", "target", "out" }
local ALL = { "core", "profile", "world", "scan", "learn", "target", "out" }
local INTERVAL = 0.1          -- 10 Hz
local STATUS_FILE = "sense_re/status.json"

local function rawlog(msg)
    msg = "[Sense RE] " .. tostring(msg)
    local lg = rawget(_G, "log")
    if lg and lg.info then pcall(lg.info, msg) end
end

-- 1) package.path: this file's folder (autorun\) and autorun\sense_re\
local function script_dir()
    local ok, src = pcall(function() return debug.getinfo(1, "S").source end)
    if ok and type(src) == "string" and src:sub(1, 1) == "@" then
        local dir = src:sub(2):match("^(.*[/\\])")
        if dir then return dir end
    end
    -- fallback: an existing "...autorun\?.lua" entry
    for entry in tostring(package.path):gmatch("[^;]+") do
        local d = entry:match("^(.*[/\\][Aa]utorun[/\\])%?%.lua$")
        if d then return d end
    end
    return nil
end

local function add_path(p)
    if not tostring(package.path):find(p, 1, true) then package.path = p .. ";" .. package.path end
end

local DIR = script_dir()
if DIR then
    pcall(add_path, DIR .. "?.lua")
    pcall(add_path, DIR .. "sense_re/?.lua")
else
    rawlog("could not find the autorun folder; using package.path as it is")
end

-- 2) load the modules (each in pcall). Names are "sense_re.<m>" so they never collide with other mods' modules.
local mods, errors = {}, {}
for _, m in ipairs(ALL) do
    local ok, r = pcall(require, "sense_re." .. m)
    if ok and type(r) == "function" then ok, r = pcall(r) end      -- a module may return a factory
    if ok and type(r) == "table" then
        mods[m] = r
    else
        errors[#errors + 1] = m .. ": " .. (ok and ("returned " .. type(r)) or tostring(r))
    end
end

local C = mods.core
local function log(msg)
    if C and C.log then pcall(C.log, msg) else rawlog(msg) end
end

local function write_status(st)
    local j = rawget(_G, "json")
    if j and j.dump_file then pcall(j.dump_file, STATUS_FILE, st) end
end

if #errors > 0 then
    for _, e in ipairs(errors) do log("LOAD ERROR " .. e) end
    log("not started: a module failed to load (see above). The game is not touched.")
    write_status({ ok = false, helper = VERSION, errors = errors })
    if C and C.flush then pcall(C.flush, nil, true) end
    return
end

-- 3) game key + profile
local GAME = "unknown"
do
    local ok, g = pcall(mods.profile.game_key)
    if ok and type(g) == "string" then GAME = g end
end
local P
do
    local ok, p, info = pcall(mods.profile.load, GAME, log)
    if ok and type(p) == "table" then
        P = p
        log(string.format("start %s, game=%s, profile default=%s game=%s, %d seeds, %d doors, target=%s",
            VERSION, GAME, tostring(info and info.default), tostring(info and info.game), #P.seeds, #P.doors,
            P.target and "re_path" or "none"))
    else
        log("profile load failed (" .. tostring(p) .. "), using built-in defaults")
        local ok2, p2 = pcall(mods.profile.merge, nil, nil)
        P = ok2 and p2 or { seeds = {}, doors = {}, discovery = {}, fov_axis = "v" }
        P.game = GAME
    end
end

local clock = (C and C.clock) or os.clock
local ctx = { game = GAME, profile = P, mods = mods, log = log, version = VERSION, seq = 0, now = clock(), dt = 0 }

-- each module's tick function (tick, else update / step); a module without one is skipped (logged once)
local STEPS = {}
for _, m in ipairs(ORDER) do
    local M = mods[m]
    local fn = M.tick or M.update or M.step
    if type(fn) == "function" then
        STEPS[#STEPS + 1] = { name = m, fn = fn }
    else
        log("module " .. m .. " has no tick(ctx); skipped")
    end
end

-- own error counter, used only if core has no kill switch (same rule: 20 errors in 1 s -> 10 s pause)
local KS = { win = -math.huge, n = 0, until_t = -math.huge }
local function running(now)
    if C and C.running then
        local ok, r = pcall(C.running, now)
        if ok then return r ~= false end
    end
    return now >= KS.until_t
end
local function count_error(where, e)
    if C and C.err then
        if pcall(C.err, where, e) then return end
    end
    local now = clock()
    if now - KS.win >= 1 then KS.win, KS.n = now, 0 end
    KS.n = KS.n + 1
    if KS.n >= 20 then KS.until_t, KS.n = now + 10, 0; rawlog("kill switch: 20 errors in 1 s, pausing 10 s") end
    rawlog("error in " .. tostring(where) .. ": " .. tostring(e))
end

for _, m in ipairs(ALL) do
    local M = mods[m]
    if type(M.init) == "function" then
        local ok, e = pcall(M.init, ctx)
        if not ok then count_error(m .. ".init", e) end
    end
end
write_status({ ok = true, helper = VERSION, game = GAME, errors = {} })

local last = -math.huge
local function tick()
    local now = clock()
    if now - last < INTERVAL - 0.004 and now >= last then return end   -- small slack: 6 frames at 60 fps = 1 tick
    if not running(now) then return end
    ctx.dt = (last == -math.huge) and 0 or (now - last)
    last = now
    ctx.now = now
    ctx.seq = ctx.seq + 1
    for i = 1, #STEPS do
        local s = STEPS[i]
        local ok, e = pcall(s.fn, ctx)
        if not ok then
            count_error(s.name, e)
            if s.name == "world" then return end           -- no fresh player/camera this tick: skip the rest
            if s.name == "target" then ctx.target = nil end
        end
        if not running(clock()) then return end
    end
    if C and C.flush then pcall(C.flush, now) end
end

local reg_ok, reg_err = pcall(function()
    re.on_application_entry("LateUpdateBehavior", function()
        local ok, e = pcall(tick)
        if not ok then count_error("tick", e) end
    end)
end)
if not reg_ok then log("could not register the update callback: " .. tostring(reg_err)) end
