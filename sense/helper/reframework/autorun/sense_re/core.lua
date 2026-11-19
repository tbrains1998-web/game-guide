-- sense_re/core.lua - safe calls, small maths, type cache, log and the kill switch for the RE Engine Sense helper.
-- Loaded with require("sense_re.core") by the loader autorun/sense_re.lua (REFramework puts autorun/ on package.path).
--
-- Rules: read-only, no sdk.hook, no draw.*, every game call inside pcall, all names local,
-- no raw addresses kept across frames (only static type metadata is cached here), files only under reframework/data.
--
-- Public API (used by world/out and by scan/learn/profile/target/the loader):
--   C.clock()                      -> seconds (os.clock)
--   C.num(v)                       -> v when it is a finite number, else nil
--   C.safe(fn, ...) / C.safe(where, fn, ...)
--                                  -> fn's results, or nil when fn errored or the kill switch is on (error counted)
--   C.try(where, fn, ...)          -> ok, results...  (ok=false when it errored or the kill switch is on)
--   C.err(where, e)                -> counts one error (kill switch) and logs it (rate limited)
--   C.running(now?)                -> false while the kill switch pause is on (after 20 errors in 1 s: 10 s pause)
--   C.pcallm(obj, name, ...)       -> obj:call(name, ...) result or nil (a probe: failures are NOT counted)
--   C.pget(obj, field)             -> obj:get_field(field) or nil (a probe)
--   C.vec(v)                       -> {x,y,z} (array, 1-based) from a REFramework vector, or nil
--   C.quat(q)                      -> {x,y,z,w} normalised, or nil
--   C.sub(a,b) C.len(a) C.dist(a,b) C.hdist(a,b) (re: Y is up, so hdist uses x and z)
--   C.unitsPerM(P)                 -> profile units_per_m when it is a number > 0, else 1
--   C.toM(v, P)                    -> {x,y,z} metres = C.vec(v) / C.unitsPerM(P), or nil. The ONE scaling function
--                                     for every game position (player, camera, items, doors, sweep, tracked target)
--   C.tdef(name)                   -> cached sdk.find_type_definition(name) or nil
--   C.tdOf(obj) C.tdName(td) C.typeName(obj)
--   C.typeChain(td|obj)            -> cached list of full type names: the type then its parents (max 16; stops at
--                                     via.Component / via.Behavior / via.Object / System.*)
--   C.methods(td) C.fields(td)     -> cached lists {name, np, ret} / {name, type, static} over the type + parents
--   C.findMethod(td, name)         -> cached method definition (walks the parents) or nil
--   C.elements(coll, max)          -> plain Lua list of a managed array / List (max default 256)
--   C.log(msg) C.once(key, msg) C.flush(now, force)   log ring written to reframework/data/sense_re/log.txt (<= 1/s)
--   C.reset()                      -> clears the kill switch counters and the caches (tests, area load)

local C = {}

C.VERSION = "0.1"
C.TAG = "[Sense RE] "
C.LOG_FILE = "sense_re/log.txt"
C.KILL_ERRORS = 20      -- errors within one second that trip the kill switch
C.KILL_PAUSE = 10       -- seconds the helper stays off after the kill switch tripped

local sqrt, floor, huge = math.sqrt, math.floor, math.huge
local unpack = table.unpack
local pack = table.pack

function C.clock()
    local ok, t = pcall(os.clock)
    if ok and type(t) == "number" then return t end
    return 0
end

function C.num(v)
    if type(v) == "number" and v == v and v ~= huge and v ~= -huge then return v end
    return nil
end

---------------------------------------------------------------------------------------------------------------------
-- log (REFramework log + ring buffer flushed to reframework/data/sense_re/log.txt at most once a second)
---------------------------------------------------------------------------------------------------------------------
local LG = { lines = {}, dirty = false, nextFlush = 0, keep = 30, max = 400, once = {} }

function C.log(msg)
    msg = tostring(msg)
    if log and log.info then pcall(log.info, C.TAG .. msg) end
    local lines = LG.lines
    if #lines >= LG.max then table.remove(lines, LG.keep + 1) end   -- keep the start-up part at the top
    lines[#lines + 1] = string.format("[%.1f] %s", C.clock(), msg)
    LG.dirty = true
end

function C.once(key, msg)
    key = tostring(key)
    if LG.once[key] then return false end
    LG.once[key] = true
    C.log(msg)
    return true
end

function C.flush(now, force)
    if not LG.dirty then return false end
    now = now or C.clock()
    if not force and now < LG.nextFlush then return false end
    LG.nextFlush, LG.dirty = now + 1, false
    if not (fs and fs.write) then return false end
    local ok = pcall(fs.write, C.LOG_FILE, table.concat(LG.lines, "\n") .. "\n")
    return ok
end

function C.logLines() return LG.lines end   -- tests

---------------------------------------------------------------------------------------------------------------------
-- kill switch: after KILL_ERRORS errors within one second the helper stops for KILL_PAUSE seconds
---------------------------------------------------------------------------------------------------------------------
local KS = { winStart = -huge, count = 0, pausedUntil = -huge, trips = 0, total = 0, lastLog = {} }

function C.running(now)
    now = now or C.clock()
    return now >= KS.pausedUntil
end

function C.err(where, e)
    local now = C.clock()
    where = tostring(where or "?")
    KS.total = KS.total + 1
    if now - KS.winStart >= 1 then KS.winStart, KS.count = now, 0 end
    KS.count = KS.count + 1
    -- one log line per place at most every 5 s (an error every frame must not flood the log)
    local last = KS.lastLog[where]
    if not last or now - last >= 5 then
        KS.lastLog[where] = now
        C.log("error in " .. where .. ": " .. tostring(e))
    end
    if KS.count >= C.KILL_ERRORS and now >= KS.pausedUntil then
        KS.pausedUntil = now + C.KILL_PAUSE
        KS.trips = KS.trips + 1
        KS.count = 0
        C.log(string.format("kill switch: %d errors in 1 s - pausing for %d s (trip %d)", C.KILL_ERRORS,
            C.KILL_PAUSE, KS.trips))
    end
end

function C.killState()   -- tests / status
    return { count = KS.count, pausedUntil = KS.pausedUntil, trips = KS.trips, total = KS.total }
end

function C.try(where, fn, ...)
    if type(fn) ~= "function" then return false end
    if not C.running() then return false end
    local r = pack(pcall(fn, ...))
    if not r[1] then
        C.err(where, r[2])
        return false
    end
    return unpack(r, 1, r.n)
end

-- C.safe(fn, ...) or C.safe("where", fn, ...): fn's results, nil on error / while paused
function C.safe(a, ...)
    local r
    if type(a) == "function" then
        r = pack(C.try("safe", a, ...))
    else
        r = pack(C.try(a, ...))
    end
    if not r[1] then return nil end
    return unpack(r, 2, r.n)
end

---------------------------------------------------------------------------------------------------------------------
-- probes (failures are normal: a method or field may not exist; they are not counted as errors)
---------------------------------------------------------------------------------------------------------------------
function C.pcallm(o, name, ...)
    if o == nil or type(name) ~= "string" then return nil end
    local a = pack(...)
    local ok, v = pcall(function() return o:call(name, unpack(a, 1, a.n)) end)
    if ok then return v end
    return nil
end

function C.pget(o, f)
    if o == nil or type(f) ~= "string" then return nil end
    local ok, v = pcall(function() return o:get_field(f) end)
    if ok then return v end
    return nil
end

---------------------------------------------------------------------------------------------------------------------
-- vectors (plain 1-based arrays; REFramework values are converted at the edge)
---------------------------------------------------------------------------------------------------------------------
function C.vec(v)
    if v == nil then return nil end
    if type(v) == "table" and v[1] ~= nil then
        local x, y, z = C.num(v[1]), C.num(v[2]), C.num(v[3])
        if x and y and z then return { x, y, z } end
        return nil
    end
    local ok, x, y, z = pcall(function() return v.x, v.y, v.z end)
    if not ok then return nil end
    x, y, z = C.num(x), C.num(y), C.num(z)
    if x and y and z then return { x, y, z } end
    return nil
end

function C.quat(q)
    if q == nil then return nil end
    local x, y, z, w
    if type(q) == "table" and q[1] ~= nil then
        x, y, z, w = q[1], q[2], q[3], q[4]
    else
        local ok, a, b, c, d = pcall(function() return q.x, q.y, q.z, q.w end)
        if not ok then return nil end
        x, y, z, w = a, b, c, d
    end
    x, y, z, w = C.num(x), C.num(y), C.num(z), C.num(w)
    if not (x and y and z and w) then return nil end
    local n = sqrt(x * x + y * y + z * z + w * w)
    if n ~= n or n < 0.5 or n > 1.5 then return nil end
    return { x / n, y / n, z / n, w / n }
end

-- units : every position read from the game goes through C.toM, so all positions use
-- the same scale (a profile with units_per_m = 100 means the game works in centimetres)
function C.unitsPerM(P)
    local u = type(P) == "table" and C.num(P.units_per_m) or nil
    if u and u > 0 then return u end
    return 1
end

function C.toM(v, P)
    local p = C.vec(v)
    if not p then return nil end
    local u = C.unitsPerM(P)
    if u ~= 1 then p = { p[1] / u, p[2] / u, p[3] / u } end
    return p
end

function C.sub(a, b) return { a[1] - b[1], a[2] - b[2], a[3] - b[3] } end
function C.len(a) return sqrt(a[1] * a[1] + a[2] * a[2] + a[3] * a[3]) end
function C.dist(a, b)
    local dx, dy, dz = a[1] - b[1], a[2] - b[2], a[3] - b[3]
    return sqrt(dx * dx + dy * dy + dz * dz)
end
function C.hdist(a, b)   -- horizontal distance; re coords are Y up
    local dx, dz = a[1] - b[1], a[3] - b[3]
    return sqrt(dx * dx + dz * dz)
end

---------------------------------------------------------------------------------------------------------------------
-- type cache (only static type metadata: type and method definitions never move while the game runs)
---------------------------------------------------------------------------------------------------------------------
local TC = { tdefs = {}, chains = {}, methods = {}, fields = {}, findm = {} }
local NONE = {}   -- cached "not found"

function C.tdName(td)
    if td == nil then return nil end
    local ok, n = pcall(function() return td:get_full_name() end)
    if ok and type(n) == "string" then return n end
    return nil
end

function C.tdOf(o)
    if o == nil then return nil end
    local ok, td = pcall(function() return o:get_type_definition() end)
    if ok then return td end
    return nil
end

function C.typeName(o) return C.tdName(C.tdOf(o)) end

function C.tdef(name)
    if type(name) ~= "string" or name == "" then return nil end
    local c = TC.tdefs[name]
    if c == NONE then return nil end
    if c ~= nil then return c end
    local td = nil
    if sdk and sdk.find_type_definition then
        local ok, r = pcall(sdk.find_type_definition, name)
        if ok then td = r end
    end
    TC.tdefs[name] = td or NONE
    return td
end

local function isStopType(n, depth)
    if depth == 0 then return false end
    return n:find("^System%.") ~= nil or n == "via.Behavior" or n == "via.Component" or n == "via.Object"
end

-- td and its parents as definitions (not cached: used by the cached builders below)
local function eachType(td, fn)
    local t, depth = td, 0
    while t ~= nil and depth < 16 do
        local n = C.tdName(t) or ""
        if isStopType(n, depth) then break end
        fn(t, n)
        local ok, p = pcall(function() return t:get_parent_type() end)
        t, depth = ok and p or nil, depth + 1
    end
end

local function asTd(x)
    if x == nil then return nil end
    if type(x) == "string" then return C.tdef(x) end
    -- a managed object answers get_type_definition (asked first: on a managed object an unknown name like
    -- get_full_name would be tried as a game method); a type definition answers get_full_name
    local td = C.tdOf(x)
    if td ~= nil and C.tdName(td) then return td end
    local okN, n = pcall(function() return x:get_full_name() end)
    if okN and type(n) == "string" then return x end
    return nil
end

function C.typeChain(x)
    local td = asTd(x)
    local key = C.tdName(td)
    if not key then return {} end
    local c = TC.chains[key]
    if c then return c end
    c = {}
    eachType(td, function(_, n) if n ~= "" then c[#c + 1] = n end end)
    TC.chains[key] = c
    return c
end

function C.methods(x)
    local td = asTd(x)
    local key = C.tdName(td)
    if not key then return {} end
    local c = TC.methods[key]
    if c then return c end
    c = {}
    eachType(td, function(t, owner)
        local okM, ml = pcall(function() return t:get_methods() end)
        if okM and type(ml) == "table" then
            for _, m in ipairs(ml) do
                local okN, nm = pcall(function() return m:get_name() end)
                local okP, np = pcall(function() return m:get_num_params() end)
                local okR, rt = pcall(function() return m:get_return_type():get_full_name() end)
                if okN and type(nm) == "string" then
                    c[#c + 1] = { name = nm, np = okP and np or -1, ret = okR and rt or "?", owner = owner }
                end
            end
        end
    end)
    TC.methods[key] = c
    return c
end

function C.fields(x)
    local td = asTd(x)
    local key = C.tdName(td)
    if not key then return {} end
    local c = TC.fields[key]
    if c then return c end
    c = {}
    eachType(td, function(t, owner)
        local okF, fl = pcall(function() return t:get_fields() end)
        if okF and type(fl) == "table" then
            for _, f in ipairs(fl) do
                local okN, nm = pcall(function() return f:get_name() end)
                local okT, tn = pcall(function() return f:get_type():get_full_name() end)
                local okS, st = pcall(function() return f:is_static() end)
                if okN and type(nm) == "string" then
                    c[#c + 1] = { name = nm, type = okT and tn or "?", static = (okS and st == true) or false,
                                  owner = owner }
                end
            end
        end
    end)
    TC.fields[key] = c
    return c
end

function C.findMethod(x, name)
    local td = asTd(x)
    local key = C.tdName(td)
    if not key or type(name) ~= "string" then return nil end
    local ck = key .. "::" .. name
    local c = TC.findm[ck]
    if c == NONE then return nil end
    if c ~= nil then return c end
    local found = nil
    eachType(td, function(t)
        if found then return end
        local okM, m = pcall(function() return t:get_method(name) end)
        if okM and m ~= nil then found = m end
    end)
    TC.findm[ck] = found or NONE
    return found
end

---------------------------------------------------------------------------------------------------------------------
-- collections
---------------------------------------------------------------------------------------------------------------------
function C.elements(coll, max)
    local out = {}
    if coll == nil then return out end
    max = max or 256
    local okE, els = pcall(function() return coll:get_elements() end)
    if okE and type(els) == "table" then
        for _, e in ipairs(els) do
            if e ~= nil then out[#out + 1] = e end
            if #out >= max then break end
        end
        return out
    end
    local okS, n = pcall(function() return coll:get_size() end)
    if okS and type(n) == "number" then
        for i = 0, math.min(n, max) - 1 do
            local okI, e = pcall(function() return coll:get_element(i) end)
            if okI and e ~= nil then out[#out + 1] = e end
        end
        return out
    end
    local okC, cnt = pcall(function() return coll:call("get_Count") end)
    if okC and type(cnt) == "number" then
        for i = 0, math.min(cnt, max) - 1 do
            local okI, e = pcall(function() return coll:call("get_Item", i) end)
            if okI and e ~= nil then out[#out + 1] = e end
        end
    end
    return out
end

function C.reset()
    KS.winStart, KS.count, KS.pausedUntil, KS.trips, KS.total, KS.lastLog = -huge, 0, -huge, 0, 0, {}
    TC.tdefs, TC.chains, TC.methods, TC.fields, TC.findm = {}, {}, {}, {}, {}
end

C.floor = floor
return C
