-- sense_re/target.lua  (Game Guide Sense helper for RE Engine games)
-- Picks the quest-path target, in this order:
--   (a) what the game tracks: the profile's "re_path" (a walk from a managed singleton through getters/fields
--       to a position); every step in pcall, read-only, nothing kept across frames;
--   (b) else the most recently UNLOCKED door/gate seen this session that the player has not reached yet
--       (reached = within 3 m horizontal AND 2.5 m vertical; RE coords: Y is up, so horizontal = x/z;
--       events older than 60 min are forgotten);
--   (c) else none.
-- Public API:
--   M.read_tracked(P, now)                          -> target|nil
--   M.walk(steps)                                   -> value|nil      (one re_path, all in pcall)
--   M.to_pos(v, P?)                                 -> {x,y,z}|nil metres (Vector3f / get_Position / Transform /
--                                                      GameObject / component; divided by P.units_per_m via core.toM)
--   M.unlocked_target(door_events, player_pos, now) -> target|nil
--   M.choose(tracked, unlocked)                     -> target|nil
--   M.tick(ctx)   reads ctx.profile, ctx.now, ctx.player, ctx.door_events; sets ctx.target (target|nil)
--   M.reset()     forget reached doors + cached tracked value (tests / area change)
-- target = { pos={x,y,z}, has_z=true, kind="quest"|"lock"|"door"|"pin", id="str", label="str",
--            src="tracked"|"unlocked" }   (pos is a dense Lua list -> JSON array, metres, Y up)

local M = {}

M.REACH_H = 3.0          -- metres, horizontal (x/z)
M.REACH_V = 2.5          -- metres, vertical (y)
M.FORGET_S = 3600        -- forget unlock events after 60 min
M.TRACK_EVERY_S = 0.5    -- re-read the tracked target twice a second

local reached = {}       -- door id -> unlock time (event t) of the newest event reached this session
local last_read_t = nil
local last_tracked = nil -- plain table copy (no game objects)
local logged = {}

local function finite(v) return type(v) == "number" and v == v and v ~= math.huge and v ~= -math.huge end

local function log_once(ctx, key, msg)
    if logged[key] then return end
    logged[key] = true
    local f = ctx and ctx.log
    if type(f) == "function" then pcall(f, msg) end
end

-- {x,y,z} | {x=,y=,z=} | {pos=...} -> x, y, z (numbers) or nil
local function xyz(v)
    if type(v) ~= "table" then return nil end
    if v.pos ~= nil and v[1] == nil and v.x == nil then v = v.pos end
    if type(v) ~= "table" then return nil end
    local x, y, z = v[1], v[2], v[3]
    if x == nil then x, y, z = v.x, v.y, v.z end
    if finite(x) and finite(y) and finite(z) then return x, y, z end
    return nil
end
M.xyz = xyz

-- read .x .y .z from a game vector (userdata or table); nil when not a vector
local function vec_of(v)
    if v == nil then return nil end
    local ok, x, y, z = pcall(function() return v.x, v.y, v.z end)
    if ok and finite(x) and finite(y) and finite(z) then return { x, y, z } end
    return nil
end

local function mcall(obj, name, ...)
    if obj == nil then return nil end
    local args = table.pack(...)
    local ok, r = pcall(function() return obj:call(name, table.unpack(args, 1, args.n)) end)
    if ok then return r end
    return nil
end

local function mfield(obj, name)
    if obj == nil then return nil end
    local ok, r = pcall(function() return obj:get_field(name) end)
    if ok then return r end
    return nil
end

-- game units -> metres with the shared core.toM (same scale as player/items).
-- The local fallback only runs when core is a stand-in without toM (unit tests); it does the same sum.
local function to_m(p, P)
    if p == nil then return nil end
    local okC, C = pcall(require, "sense_re.core")
    if okC and type(C) == "table" and type(C.toM) == "function" then return C.toM(p, P) end
    local u = type(P) == "table" and P.units_per_m or nil
    if finite(u) and u > 0 and u ~= 1 then return { p[1] / u, p[2] / u, p[3] / u } end
    return p
end

-- P (optional) = merged profile, for units_per_m
function M.to_pos(v, P)
    if v == nil then return nil end
    local p = vec_of(v)
    if p then return to_m(p, P) end
    p = vec_of(mcall(v, "get_Position"))      -- v is a Transform
    if p then return to_m(p, P) end
    local tr = mcall(v, "get_Transform")
    if tr == nil then
        local go = mcall(v, "get_GameObject")
        if go ~= nil then tr = mcall(go, "get_Transform") end
    end
    if tr ~= nil then return to_m(vec_of(mcall(tr, "get_Position")), P) end
    return nil
end

local function singleton(name)
    local s = rawget(_G, "sdk")
    if s == nil then return nil end
    local ok, r = pcall(function() return s.get_managed_singleton(name) end)
    if ok then return r end
    return nil
end

local function list_item(obj, i)
    local n = mcall(obj, "get_Count")
    if finite(n) then
        if i >= n then return nil end
        return mcall(obj, "get_Item", i)
    end
    n = mcall(obj, "get_size")
    if finite(n) then
        if i >= n then return nil end
        return mcall(obj, "get_element", i)
    end
    return nil
end

function M.walk(steps)
    if type(steps) ~= "table" then return nil end
    local cur = nil
    for i = 1, #steps do
        local s = steps[i]
        if type(s) ~= "table" then return nil end
        if s.single ~= nil then cur = singleton(s.single)
        elseif s.call ~= nil then cur = mcall(cur, s.call)
        elseif s.field ~= nil then cur = mfield(cur, s.field)
        elseif s.item ~= nil then cur = list_item(cur, math.floor(s.item))
        else return nil end
        if cur == nil then return nil end
    end
    return cur
end

function M.read_tracked(P, now)
    local spec = type(P) == "table" and P.target or nil
    if type(spec) ~= "table" or spec.type ~= "re_path" or type(spec.paths) ~= "table" then return nil end
    for i = 1, #spec.paths do
        local ok, v = pcall(M.walk, spec.paths[i])
        local pos = ok and M.to_pos(v, P) or nil
        -- (0,0,0) = "nothing set" in most RE managers; never a real quest spot
        if pos and not (pos[1] == 0 and pos[2] == 0 and pos[3] == 0) then
            return { pos = pos, has_z = true, kind = spec.kind or "quest", id = spec.id or "tracked",
                     label = spec.label or "", src = "tracked", path = i }
        end
    end
    return nil
end

local function each_event(evs, fn)
    if type(evs) ~= "table" then return end
    if #evs > 0 then
        for i = 1, #evs do fn(evs[i], i) end
    else
        for k, e in pairs(evs) do fn(e, k) end
    end
end

-- door_events: list (or map) of { id="str", pos={x,y,z}|{x=,y=,z=}, t=secs (unlock time, same clock as now),
--                                kind?="door"|"lock", label?="", cls?="app.GmDoorV3" }
function M.unlocked_target(door_events, player_pos, now)
    if not finite(now) then return nil end
    local px, py, pz = xyz(player_pos)
    local best, best_t, best_pos, best_id = nil, nil, nil, nil
    each_event(door_events, function(e, key)
        if type(e) ~= "table" then return end
        local id = e.id ~= nil and tostring(e.id) or tostring(key)
        if e.reached then return end
        local t = e.t
        if not finite(t) or now - t > M.FORGET_S then return end
        -- reached[id] = time of the unlock event that was reached; only that event and older ones are skipped,
        -- so a newer unlock of the same door (locked again, unlocked again) counts again
        if reached[id] ~= nil and t <= reached[id] then return end
        local x, y, z = xyz(e.pos)
        if not x then return end
        if px then
            local dx, dz = x - px, z - pz
            if dx * dx + dz * dz <= M.REACH_H * M.REACH_H and math.abs(y - py) <= M.REACH_V then
                if reached[id] == nil or t > reached[id] then reached[id] = t end
                return
            end
        end
        if best_t == nil or t > best_t then best, best_t, best_pos, best_id = e, t, { x, y, z }, id end
    end)
    if not best then return nil end
    local kind = (best.kind == "lock" or best.kind == "door" or best.kind == "quest") and best.kind or "door"
    local label = type(best.label) == "string" and best.label or ""
    return { pos = best_pos, has_z = true, kind = kind, id = best_id, label = label, src = "unlocked" }
end

function M.choose(tracked, unlocked)
    if type(tracked) == "table" then return tracked end
    if type(unlocked) == "table" then return unlocked end
    return nil
end

local function door_events_of(ctx)
    if type(ctx.door_events) == "table" then return ctx.door_events end
    if type(ctx.learn) == "table" and type(ctx.learn.door_events) == "table" then return ctx.learn.door_events end
    return nil
end

function M.tick(ctx)
    if type(ctx) ~= "table" then return nil end
    local now = ctx.now
    if not finite(now) then now = os.clock() end
    local P = ctx.profile
    if type(P) == "table" and P.target then
        if last_read_t == nil or now - last_read_t >= M.TRACK_EVERY_S or now < last_read_t then
            last_read_t = now
            local ok, t = pcall(M.read_tracked, P, now)
            last_tracked = ok and t or nil
            if last_tracked then
                log_once(ctx, "tracked" .. tostring(last_tracked.path),
                    "target: tracked target read through profile path #" .. tostring(last_tracked.path))
            end
        end
    else
        last_tracked = nil
    end
    local unl = M.unlocked_target(door_events_of(ctx), ctx.player, now)
    if unl then log_once(ctx, "unlocked", "target: using the last unlocked door (no tracked target)") end
    local t = M.choose(last_tracked, unl)
    if t then t = { pos = { t.pos[1], t.pos[2], t.pos[3] }, has_z = true, kind = t.kind, id = t.id,
                    label = t.label, src = t.src } end
    ctx.target = t
    return t
end

function M.reset()
    reached = {}
    last_read_t, last_tracked = nil, nil
    logged = {}
end

function M.is_reached(id) return reached[tostring(id)] ~= nil end

return M
