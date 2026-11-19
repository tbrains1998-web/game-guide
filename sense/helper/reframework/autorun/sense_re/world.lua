-- sense_re/world.lua - scene, player, camera and screen for the RE Engine Sense helper (read-only).
-- Loaded with require("sense_re.world"). Player search is a scored search (player managers, getters whose
-- names say "player", the camera's target, the DD2 hint), plus an optional profile hint. Only the WAY to the
-- player (singleton + getter name) is kept between frames, never the objects: every tick goes
-- singleton -> getter -> transform -> position again.
--
-- Public API:
--   W.gameName()                 -> "dd2" / "re9" / ... (reframework:get_game_name(), lower case; "re" if unknown)
--   W.scene()                    -> the current via.Scene (fresh every call) or nil
--   W.findComponents(typeName, max) -> plain list of components of that type in the current scene (max 300) or {}
--   W.transformOf(o)             -> via.Transform of a GameObject / component / owner object, or nil
--   W.posOf(o)                   -> {x,y,z} metres of that object's transform, or nil
--   W.player(P, now)             -> nil | { pos={x,y,z}, foot_dz=number, how="text", type="type name" }
--                                   P = profile table (P.player_hint optional, P.player_foot_dz optional,
--                                   P.units_per_m optional, default 1)
--   W.camera(P)                  -> nil | { pos={x,y,z}, quat={x,y,z,w}, fov=degrees }  (camera looks local -Z)
--   W.screen(now)                -> {w,h} pixels (cached 2 s); {0,0} when unknown (Game Guide then uses its monitor)
--   W.reset()                    -> forget the player route and the screen cache (area load, tests)

local C = require("sense_re.core")

local W = {}

local lower = function(s) return type(s) == "string" and s:lower() or "" end
local huge = math.huge

W.SEARCH_EVERY = 2.0        -- seconds between full player searches while no player is known
W.SCREEN_EVERY = 2.0
W.CAM_GATE = 60             -- a player candidate farther than this from the camera is ignored (metres)
W.DEFAULT_FOV = 60

---------------------------------------------------------------------------------------------------------------------
-- game, scene, components
---------------------------------------------------------------------------------------------------------------------
local GAME = nil
function W.gameName()
    if GAME then return GAME end
    local name = "re"
    -- same key as the loader (data/sense_re/game.txt first, then get_game_name)
    local okP, Pm = pcall(require, "sense_re.profile")
    if okP and type(Pm) == "table" and type(Pm.game_key) == "function" then
        local okK, k = pcall(Pm.game_key)
        if okK and type(k) == "string" and k ~= "" and k ~= "unknown" then GAME = k; return k end
    end
    if reframework and reframework.get_game_name then
        local ok, n = pcall(function() return reframework:get_game_name() end)
        if ok and type(n) == "string" and n ~= "" then name = n:lower() end
    end
    GAME = name
    return name
end

function W.scene()
    if not (sdk and sdk.get_native_singleton and sdk.call_native_func) then return nil end
    local okS, sm = pcall(sdk.get_native_singleton, "via.SceneManager")
    if not okS or sm == nil then return nil end
    local td = C.tdef("via.SceneManager")
    if td == nil then return nil end
    local ok, sc = pcall(sdk.call_native_func, sm, td, "get_CurrentScene()")
    if ok then return sc end
    return nil
end

function W.findComponents(typeName, max)
    max = max or 300
    if type(typeName) ~= "string" or not (sdk and sdk.typeof) then return {} end
    local sc = W.scene()
    if sc == nil then return {} end
    local okT, ty = pcall(sdk.typeof, typeName)
    if not okT or ty == nil then return {} end
    local arr = C.pcallm(sc, "findComponents(System.Type)", ty)
    return C.elements(arr, max)
end

---------------------------------------------------------------------------------------------------------------------
-- transforms (the way from an object type to its transform is cached per type name; the objects are not)
---------------------------------------------------------------------------------------------------------------------
local ROUTE = {}   -- type name -> { kind = "self"|"go"|"getgo"|"method"|"field", name=, ret= } or false

local function routeFor(o, tn)
    if tn == "via.Transform" then return { kind = "self" } end
    if tn == "via.GameObject" then return { kind = "go" } end
    local go = C.pcallm(o, "get_GameObject")
    if go ~= nil and C.pcallm(go, "get_Transform") ~= nil then return { kind = "getgo" } end
    local bestM, bestS
    for _, m in ipairs(C.methods(tn)) do
        if m.np == 0 and (m.ret == "via.GameObject" or m.ret == "via.Transform") then
            local n = lower(m.name)
            if n:find("^get") then
                local s = (n:find("body", 1, true) and 3 or 0) + (n:find("owner", 1, true) and 2 or 0) + 1
                if not bestS or s > bestS then bestM, bestS = m, s end
            end
        end
    end
    if bestM then return { kind = "method", name = bestM.name, ret = bestM.ret } end
    for _, f in ipairs(C.fields(tn)) do
        if not f.static and (f.type == "via.Transform" or f.type == "via.GameObject") then
            return { kind = "field", name = f.name, ret = f.type }
        end
    end
    return false
end

local function follow(o, r)
    if r.kind == "self" then return o end
    if r.kind == "go" then return C.pcallm(o, "get_Transform") end
    if r.kind == "getgo" then
        local go = C.pcallm(o, "get_GameObject")
        return go and C.pcallm(go, "get_Transform") or nil
    end
    local v
    if r.kind == "method" then v = C.pcallm(o, r.name) else v = C.pget(o, r.name) end
    if v == nil then return nil end
    if r.ret == "via.Transform" then return v end
    return C.pcallm(v, "get_Transform")
end

function W.transformOf(o)
    if o == nil then return nil end
    local tn = C.typeName(o)
    if not tn then return nil end
    local r = ROUTE[tn]
    if r == nil then
        r = routeFor(o, tn)
        ROUTE[tn] = r
    end
    if r == false then return nil end
    return follow(o, r)
end

-- scaling: the shared C.toM / C.unitsPerM (scan and target use the same function)
local function units(P) return C.unitsPerM(P) end

local function trPos(tr, P)
    return C.toM(C.pcallm(tr, "get_Position"), P)
end

function W.posOf(o, P)
    local tr = W.transformOf(o)
    if tr == nil then return nil end
    return trPos(tr, P)
end

---------------------------------------------------------------------------------------------------------------------
-- camera
---------------------------------------------------------------------------------------------------------------------
local function primaryCamera()
    if not (sdk and sdk.get_primary_camera) then return nil end
    local ok, cam = pcall(sdk.get_primary_camera)
    if ok then return cam end
    return nil
end

function W.camera(P)
    local cam = primaryCamera()
    if cam == nil then return nil end
    local go = C.pcallm(cam, "get_GameObject")
    local tr = go and C.pcallm(go, "get_Transform")
    if tr == nil then return nil end
    local pos = trPos(tr, P)
    local q = C.quat(C.pcallm(tr, "get_Rotation"))
    if not (pos and q) then return nil end
    local fov = C.num(C.pcallm(cam, "get_FOV"))
    if not fov or fov < 1 or fov > 170 then
        C.once("nofov", "camera: get_FOV gave no usable value - sending " .. W.DEFAULT_FOV .. " degrees")
        fov = W.DEFAULT_FOV
    end
    return { pos = pos, quat = q, fov = fov }
end

---------------------------------------------------------------------------------------------------------------------
-- screen
---------------------------------------------------------------------------------------------------------------------
local SCR = { next = -huge, val = { 0, 0 } }

local function sizeOf(v)
    if v == nil then return nil end
    local ok, x, y = pcall(function() return v.x, v.y end)
    if not (ok and C.num(x) and C.num(y)) then
        ok, x, y = pcall(function() return v.w, v.h end)
    end
    if ok and C.num(x) and C.num(y) and x > 0 and y > 0 then return { math.floor(x + 0.5), math.floor(y + 0.5) } end
    return nil
end

function W.screen(now)
    now = now or C.clock()
    if now < SCR.next then return SCR.val end
    SCR.next = now + W.SCREEN_EVERY
    local s = nil
    if imgui and imgui.get_display_size then
        local ok, ds = pcall(imgui.get_display_size)
        if ok then s = sizeOf(ds) end
    end
    if not s and sdk and sdk.get_native_singleton and sdk.call_native_func then
        local okS, sm = pcall(sdk.get_native_singleton, "via.SceneManager")
        local td = C.tdef("via.SceneManager")
        if okS and sm ~= nil and td ~= nil then
            local okV, view = pcall(sdk.call_native_func, sm, td, "get_MainView()")
            if okV and view ~= nil then s = sizeOf(C.pcallm(view, "get_WindowSize")) end
        end
    end
    SCR.val = s or { 0, 0 }
    return SCR.val
end

---------------------------------------------------------------------------------------------------------------------
-- player (scored search; only "get*" getters with no parameters are ever called)
---------------------------------------------------------------------------------------------------------------------
W.PLAYER_MANAGERS = { "CharacterManager", "PlayerManager", "SurvivorManager", "GameManager", "PlayerSystem",
    "CharacterSystem" }
W.BAD_PLAYER = { "list", "array", "count", "num", "index", "name", "flag", "param", "data", "info", "setting",
    "pawn", "npc", "enemy", "dead", "history", "old", "prev", "last", "spawn", "request", "gui", "hud", "camera",
    "input", "id", "level", "save", "status", "option", "callback", "event" }
W.GOOD_PLAYER = { "manualplayer", "masterplayer", "currentplayer", "mainplayer", "localplayer", "controlplayer",
    "playerself", "myplayer" }
W.HEAD_NAMES = { "Head_0", "head", "Head", "J_Head", "j_head", "Head_00", "head_0", "HEAD", "C_Head", "c_head",
    "Head01", "head01", "Head1", "Head_1" }

local function nsName(n)
    if sdk and sdk.game_namespace then
        local ok, s = pcall(sdk.game_namespace, n)
        if ok and type(s) == "string" and #s > 0 then return s end
    end
    return "app." .. n
end

local function isObjectType(t)
    if type(t) ~= "string" or t == "?" then return false end
    if t:find("^System%.") or t:find("%[%]$") then return false end
    if t:find("^via%.vec") or t:find("^via%.Quaternion") or t:find("^via%.Position") or t:find("^via%.Float") then
        return false
    end
    return t:find("%.") ~= nil
end

function W.playerScore(ln, isMethod)
    if isMethod and not ln:find("^get") then return nil end
    if ln:find("^set") or ln:find("^add") or ln:find("^remove") or not ln:find("player", 1, true) then return nil end
    for _, w in ipairs(W.BAD_PLAYER) do
        if w == "id" then
            if ln:find("id$") then return nil end
        elseif ln:find(w, 1, true) then
            return nil
        end
    end
    local s = 10
    for _, w in ipairs(W.GOOD_PLAYER) do if ln:find(w, 1, true) then s = s + 50; break end end
    if ln:find("player$") or ln:find("playercontext") then s = s + 10 end
    if ln:find("^get") then s = s + 5 end
    return s
end

local function singleton(name)
    if not (sdk and sdk.get_managed_singleton) or type(name) ~= "string" then return nil end
    local ok, s = pcall(sdk.get_managed_singleton, name)
    if ok then return s end
    return nil
end

local function hasHead(tr)
    for _, n in ipairs(W.HEAD_NAMES) do
        local j = C.pcallm(tr, "getJointByName", n)
        if j ~= nil and C.vec(C.pcallm(j, "get_Position")) then return n end
    end
    return nil
end

-- "app.CharacterManager.get_ManualPlayer" / "app.CharacterManager:get_ManualPlayer" / {single=, method=|field=}
local function parseHint(h)
    if type(h) == "table" then
        if type(h.single) == "string" and (type(h.method) == "string" or type(h.field) == "string") then
            return { single = h.single, kind = h.method and "method" or "field", name = h.method or h.field }
        end
        return nil
    end
    if type(h) ~= "string" or h == "" then return nil end
    local s, n = h:match("^(.-)[:](.+)$")
    if not s then s, n = h:match("^(.+)%.([^%.]+)$") end
    if not (s and n and s ~= "" and n ~= "") then return nil end
    n = n:gsub("%(%)$", "")
    return { single = s, kind = "auto", name = n }
end

-- getter description -> the object it returns now (nil when it is gone)
local function resolve(g)
    if g == nil then return nil end
    if g.cam then
        local cam = primaryCamera()
        local go = cam and C.pcallm(cam, "get_GameObject")
        if go == nil or not (sdk and sdk.typeof) then return nil end
        local okT, ty = pcall(sdk.typeof, g.compType)
        if not okT or ty == nil then return nil end
        local comp = C.pcallm(go, "getComponent(System.Type)", ty)
        return comp and C.pcallm(comp, g.name) or nil
    end
    local s = singleton(g.single)
    if s == nil then return nil end
    if g.kind == "field" then return C.pget(s, g.name) end
    if g.kind == "method" then return C.pcallm(s, g.name) end
    local v = C.pcallm(s, g.name)          -- "auto" (profile hint): a getter, else a field
    if v == nil then v = C.pget(s, g.name) end
    return v
end

local function candidates(P)
    local cands = {}
    local camPos = nil
    do
        local cam = W.camera(P)
        camPos = cam and cam.pos
    end
    local function consider(o, score, how, getter, noGate)
        if o == nil then return end
        local tr = W.transformOf(o)
        if tr == nil then return end
        local root = trPos(tr, P)
        if not root then return end
        if not noGate and camPos and C.dist(root, camPos) > W.CAM_GATE then return end
        cands[#cands + 1] = { tr = tr, root = root, score = score, how = how, getter = getter,
                              typeName = C.typeName(o) or "?" }
    end
    -- 0) profile hint (always wins when it gives a transform)
    local hint = parseHint(P and P.player_hint)
    if hint then consider(resolve(hint), 1000, "profile hint " .. tostring(P.player_hint), hint, true) end
    -- a) generic: player getters / fields on *CharacterManager-like singletons
    local names, seen = {}, {}
    for _, s in ipairs(W.PLAYER_MANAGERS) do
        local n = nsName(s)
        if not seen[n] then seen[n] = true; names[#names + 1] = n end
    end
    for _, mn in ipairs(names) do
        local s = singleton(mn)
        if s ~= nil then
            local nm = 0
            for _, m in ipairs(C.methods(s)) do
                if m.np == 0 and isObjectType(m.ret) then
                    local sc = W.playerScore(lower(m.name), true)
                    if sc then
                        nm = nm + 1
                        if nm > 24 then break end
                        consider(C.pcallm(s, m.name), sc, mn .. "." .. m.name .. "()",
                            { single = mn, kind = "method", name = m.name })
                    end
                end
            end
            for _, f in ipairs(C.fields(s)) do
                if not f.static and isObjectType(f.type) then
                    local sc = W.playerScore(lower((f.name:gsub("k__BackingField", ""):gsub("[<>_]", ""))))
                    if sc then
                        consider(C.pget(s, f.name), sc - 2, mn .. "." .. f.name,
                            { single = mn, kind = "field", name = f.name })
                    end
                end
            end
        end
    end
    -- b) the transform the game camera follows (get*Target* getters on the camera GameObject's components)
    local cam = primaryCamera()
    local camGO = cam and C.pcallm(cam, "get_GameObject")
    if camGO ~= nil then
        for _, comp in ipairs(C.elements(C.pcallm(camGO, "get_Components"), 32)) do
            local tn = C.typeName(comp)
            if tn then
                for _, m in ipairs(C.methods(tn)) do
                    local n = lower(m.name)
                    if m.np == 0 and (m.ret == "via.Transform" or m.ret == "via.GameObject")
                        and n:find("target", 1, true) and n:find("^get") then
                        consider(C.pcallm(comp, m.name), 8, tn .. "." .. m.name .. "()",
                            { cam = true, compType = tn, name = m.name })
                    end
                end
            end
        end
    end
    -- c) Dragon's Dogma 2 hint (after the generic search): app.CharacterManager:get_ManualPlayer()
    if #cands == 0 then
        local s = singleton("app.CharacterManager")
        if s ~= nil then
            consider(C.pcallm(s, "get_ManualPlayer"), 5, "DD2 hint app.CharacterManager.get_ManualPlayer()",
                { single = "app.CharacterManager", kind = "method", name = "get_ManualPlayer" })
        end
    end
    return cands
end

local PL = { getter = nil, how = nil, typeName = nil, nextSearch = -huge, lastKey = nil }

local function search(P, now)
    local cands = candidates(P)
    for _, c in ipairs(cands) do
        local head = hasHead(c.tr)
        if head then
            c.score = c.score + 100
            local hp = C.vec(C.pcallm(C.pcallm(c.tr, "getJointByName", head), "get_Position"))
            local u = units(P)
            if hp and u == 1 and (hp[2] - c.root[2]) > 20 then
                C.once("units_" .. c.typeName, "player " .. c.typeName .. ": head is more than 20 units above the "
                    .. "root - units may be centimetres (set units_per_m: 100 in the profile)")
            end
        end
    end
    table.sort(cands, function(a, b) return a.score > b.score end)
    local best = cands[1]
    if not best then
        if PL.getter then C.log("player lost") end
        PL.getter, PL.how, PL.typeName = nil, nil, nil
        return nil
    end
    PL.getter, PL.how, PL.typeName, PL.lost = best.getter, best.how, best.typeName, nil
    local key = tostring(best.typeName) .. "|" .. tostring(best.how)
    if PL.lastKey ~= key then
        PL.lastKey = key
        C.log(string.format("player found: %s via %s (%d candidate(s))", tostring(best.typeName), best.how, #cands))
    end
    return best.tr
end

function W.player(P, now)
    now = now or C.clock()
    local tr = nil
    if PL.getter then
        local o = resolve(PL.getter)
        tr = o and W.transformOf(o) or nil
        if tr == nil then
            C.log("player lost (" .. tostring(PL.how) .. " returned nothing)")
            PL.lost = { getter = PL.getter, how = PL.how }   -- plain route description, no game object
            PL.getter, PL.how = nil, nil
            PL.nextSearch = -huge
        end
    end
    if tr == nil and PL.lost then
        -- cheap: the last working route (one singleton + one getter) is retried every tick, so a short gap is
        -- measured as short (ctx.area counts gaps over AREA_GAP_S; the full search only runs every 2 s)
        local o = resolve(PL.lost.getter)
        tr = o and W.transformOf(o) or nil
        if tr ~= nil then
            PL.getter, PL.how, PL.lost = PL.lost.getter, PL.lost.how, nil
            C.log("player back (" .. tostring(PL.how) .. ")")
        end
    end
    if tr == nil then
        if now < PL.nextSearch then return nil end
        PL.nextSearch = now + W.SEARCH_EVERY
        tr = search(P, now)
        if tr == nil then return nil end
    end
    local pos = trPos(tr, P)
    if not pos then return nil end
    local fdz = C.num(P and P.player_foot_dz) or 0   -- RE characters: the transform sits at the feet (unverified RE9)
    return { pos = pos, foot_dz = fdz, how = PL.how, type = PL.typeName }
end

-- area: a plain counter, sent as ctx.area = "area<n>". It goes up when the player
-- comes back after being gone for more than AREA_GAP_S (a load screen, a room/door load that swaps the player,
-- a cutscene). scan.lua treats a new ctx.area as an area change (sweep owed, no "vanish" counted for the refresh).
-- Cheap: it only uses what W.player already read this tick. No game object is kept.
W.AREA_GAP_S = 1.0
local AREA = { n = 0, goneSince = nil }

function W.areaStep(hasPlayer, now)
    if hasPlayer then
        if AREA.goneSince ~= nil and now - AREA.goneSince > W.AREA_GAP_S then
            AREA.n = AREA.n + 1
            C.log(string.format("area change #%d (player was gone %.1f s)", AREA.n, now - AREA.goneSince))
        end
        AREA.goneSince = nil
    elseif AREA.goneSince == nil then
        AREA.goneSince = now
    end
    return "area" .. AREA.n
end

function W.reset()
    PL.getter, PL.how, PL.typeName, PL.nextSearch, PL.lastKey, PL.lost = nil, nil, nil, -huge, nil, nil
    SCR.next, SCR.val = -huge, { 0, 0 }
    ROUTE = {}
    GAME = nil
    AREA.n, AREA.goneSince = 0, nil
end

-- loader hook (added by the RE integration step): fills the plain per-tick data the other modules read.
--   ctx.player = {x,y,z} | nil, ctx.foot_dz, ctx.player_info = {how,type} | nil, ctx.cam = {pos,quat,fov} | nil,
--   ctx.screen = {w,h}, ctx.err = text | nil, ctx.area = "area<n>" (see W.areaStep). Nothing here keeps a game object.
function W.tick(ctx)
    if type(ctx) ~= "table" then return end
    local P = type(ctx.profile) == "table" and ctx.profile or {}
    local now = C.num(ctx.now) or C.clock()
    local pl = W.player(P, now)
    if pl and type(pl.pos) == "table" then
        ctx.player = { pl.pos[1], pl.pos[2], pl.pos[3] }
        ctx.foot_dz = pl.foot_dz
        ctx.player_info = { how = pl.how, type = pl.type }
        ctx.err = nil
    else
        ctx.player, ctx.foot_dz, ctx.player_info = nil, nil, nil
        ctx.err = "no player yet"
    end
    ctx.area = W.areaStep(ctx.player ~= nil, now)
    ctx.cam = W.camera(P)
    ctx.screen = W.screen(now)
end

return W
