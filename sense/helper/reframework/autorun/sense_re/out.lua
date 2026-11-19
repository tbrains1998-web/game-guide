-- sense_re/out.lua - builds the sense.json v2 message ("coords":"re") and writes it to reframework/data/sense/sense.json
-- at most 10 times a second. Loaded with require("sense_re.out").
--
-- Writing: the message is turned into JSON text here (dense arrays, [] for an empty list, real nulls) and written
-- with pcall(fs.write, "sense/sense.json", text). If fs.write is missing or fails, it falls back to
-- pcall(json.dump_file, "sense/sense.json", plainTable) (REFramework's own encoder; empty lists and nulls can come
-- out differently there, so it is only the fallback). The file is not swapped atomically (no os.rename in
-- REFramework); Game Guide keeps the last good message when a read is cut.
--
-- Public API:
--   O.PATH = "sense/sense.json"; O.HELPER = "reframework-sense/0.1"; O.MIN_GAP = 0.1 (s); O.MAX_ITEMS = 64
--   O.ARRAY, O.NULL, O.array(t), O.encode(v)   tiny JSON writer (only tables marked with O.array are JSON arrays)
--   O.build(st) -> message table (ready for O.encode). st fields (all optional except where said):
--       game="dd2", seq=n, t=ms, player={x,y,z}|nil, foot_dz=number|nil, cam={pos={3},quat={4},fov=deg}|nil,
--       fov_axis="v"|"h" (default "v"), screen={w,h}, range=30, items={ {id,kind,name,pos={3},dist?,state?} },
--       classes={type name -> count}, target=nil|{pos={3},has_z?,kind,id,label?,src}, learned=nil|n|{count=n},
--       err="text"|nil, debug_state=bool, max_items=n
--     No player -> "error" (st.err or "no player yet"), no player/cam/target (like the UE helper).
--     Items: kind "door" is never sent; items without a finite pos are dropped; dist is 3D from the player when
--     missing; only dist <= range; nearest first; at most max_items.
--   O.publish(st, now) -> true when written now, false when throttled (< MIN_GAP since the last write) or failed.
--     Sets seq (own counter) and t (ms since load) itself.
--   O.lastText() -> the last JSON text written (tests)

local C = require("sense_re.core")

local O = {}

O.PATH = "sense/sense.json"
O.HELPER = "reframework-sense/0.1"
O.MIN_GAP = 0.1
O.MAX_ITEMS = 64
O.RANGE = 30

local huge = math.huge

---------------------------------------------------------------------------------------------------------------------
-- JSON writer (same rules as the UE helper's: only O.array tables are arrays, O.NULL is null, keys sorted)
---------------------------------------------------------------------------------------------------------------------
local ARRAY = {}
local NULL = setmetatable({}, { __name = "null" })
O.ARRAY, O.NULL = ARRAY, NULL

function O.array(t) return setmetatable(t or {}, ARRAY) end

local function jsonString(s)
    s = s:gsub('[%c"\\]', function(c)
        if c == '"' then return '\\"' elseif c == "\\" then return "\\\\"
        elseif c == "\n" then return "\\n" elseif c == "\r" then return "\\r" elseif c == "\t" then return "\\t"
        else return string.format("\\u%04x", c:byte()) end
    end)
    return '"' .. s .. '"'
end

local function jsonNumber(n)
    if n ~= n or n == huge or n == -huge then return "null" end
    if math.type(n) == "integer" then return string.format("%d", n) end
    if n == math.floor(n) and math.abs(n) < 1e15 then return string.format("%d", math.floor(n)) end
    local s = string.format("%.4f", n)
    s = s:gsub("0+$", ""):gsub("%.$", "")
    if s == "-0" then s = "0" end
    return s
end

local function enc(v, depth)
    if depth > 32 then return "null" end
    local t = type(v)
    if v == nil or v == NULL then return "null"
    elseif t == "boolean" then return v and "true" or "false"
    elseif t == "number" then return jsonNumber(v)
    elseif t == "string" then return jsonString(v)
    elseif t == "table" then
        local parts = {}
        if getmetatable(v) == ARRAY then
            for i = 1, #v do parts[i] = enc(v[i], depth + 1) end
            return "[" .. table.concat(parts, ",") .. "]"
        end
        local keys = {}
        for k in pairs(v) do keys[#keys + 1] = tostring(k) end
        table.sort(keys)
        for i, k in ipairs(keys) do
            local val = v[k]
            if val == nil then val = v[tonumber(k)] end
            parts[i] = jsonString(k) .. ":" .. enc(val, depth + 1)
        end
        return "{" .. table.concat(parts, ",") .. "}"
    end
    return "null"
end

function O.encode(v) return enc(v, 0) end

-- a plain copy for json.dump_file (no metatables, NULL dropped)
local function plain(v, depth)
    depth = depth or 0
    if v == NULL or depth > 32 then return nil end
    if type(v) ~= "table" then return v end
    local r = {}
    for k, x in pairs(v) do
        local p = plain(x, depth + 1)
        if p ~= nil then r[k] = p end
    end
    return r
end
O.plain = plain

---------------------------------------------------------------------------------------------------------------------
-- message
---------------------------------------------------------------------------------------------------------------------
local function vec3(a)
    if type(a) ~= "table" then return nil end
    local x, y, z = C.num(a[1]), C.num(a[2]), C.num(a[3])
    if not (x and y and z) then return nil end
    return O.array({ x, y, z })
end

local function str(v, d)
    if type(v) == "string" then return v end
    if type(v) == "number" then return tostring(v) end
    return d
end

local function buildTarget(tg)
    if type(tg) ~= "table" then return NULL end
    local pos = vec3(tg.pos)
    if not pos then return NULL end
    return {
        pos = pos,
        has_z = tg.has_z ~= false,
        kind = str(tg.kind, "door"),
        id = str(tg.id, ""),
        label = str(tg.label, ""),
        src = str(tg.src, "unlocked"),
    }
end

local function buildCam(cam, axis)
    if type(cam) ~= "table" then return nil end
    local pos = vec3(cam.pos)
    local q = cam.quat
    if not pos or type(q) ~= "table" then return nil end
    local qx, qy, qz, qw = C.num(q[1]), C.num(q[2]), C.num(q[3]), C.num(q[4])
    if not (qx and qy and qz and qw) then return nil end
    local fov = C.num(cam.fov)
    if not fov or fov <= 0 or fov >= 180 then fov = 60 end
    return { pos = pos, rot = NULL, quat = O.array({ qx, qy, qz, qw }), fov = fov,
             fov_axis = (axis == "h") and "h" or "v" }
end

function O.build(st)
    st = st or {}
    local range = C.num(st.range) or O.RANGE
    local scr = type(st.screen) == "table" and st.screen or {}
    local msg = {
        v = 1,
        game = str(st.game, "re"),
        helper = O.HELPER,
        seq = math.tointeger(st.seq) or 0,
        t = math.tointeger(st.t) or 0,
        coords = "re",
        screen = O.array({ math.tointeger(scr[1]) or 0, math.tointeger(scr[2]) or 0 }),
        range = range,
    }
    local cls = {}
    if type(st.classes) == "table" then
        for k, n in pairs(st.classes) do
            local c = C.num(n)
            if c then cls[tostring(k)] = math.tointeger(c) or c end
        end
    end
    msg.classes = cls
    local lc = st.learned
    if type(lc) == "number" then lc = { count = lc } end
    if type(lc) == "table" and C.num(lc.count) then msg.learned = { count = math.tointeger(lc.count) or lc.count } end

    local player = vec3(st.player)
    local list = O.array()
    if not player then
        msg.items = list
        msg.error = str(st.err, "no player yet")
        return msg
    end
    msg.player = player
    msg.player_foot_dz = C.num(st.foot_dz) or 0
    local cam = buildCam(st.cam, st.fov_axis)
    if cam then msg.cam = cam end

    local near = {}
    for _, it in ipairs(type(st.items) == "table" and st.items or {}) do
        if type(it) == "table" and it.kind ~= "door" then
            local pos = vec3(it.pos)
            if pos then
                local d = C.num(it.dist) or C.dist(pos, player)
                if d <= range then
                    local e = { id = str(it.id, "?"), kind = str(it.kind, "pickup"), name = str(it.name, ""),
                                pos = pos, dist = d, screen = NULL }
                    if st.debug_state and type(it.state) == "table" then e.state = it.state end
                    near[#near + 1] = e
                end
            end
        end
    end
    table.sort(near, function(a, b) return a.dist < b.dist end)
    local max = math.tointeger(st.max_items) or O.MAX_ITEMS
    for i = 1, math.min(#near, max) do list[i] = near[i] end
    msg.items = list
    msg.target = buildTarget(st.target)
    if st.err then msg.error = str(st.err, nil) end
    return msg
end

---------------------------------------------------------------------------------------------------------------------
-- writing (<= 10 Hz)
---------------------------------------------------------------------------------------------------------------------
local OS = { last = -huge, seq = 0, t0 = nil, text = nil, how = nil }

local function writeText(msg, text)
    if fs and fs.write then
        local ok = pcall(fs.write, O.PATH, text)
        if ok then
            if OS.how ~= "fs" then OS.how = "fs"; C.log("sense.json written with fs.write") end
            return true
        end
    end
    if json and json.dump_file then
        local ok, r = pcall(json.dump_file, O.PATH, plain(msg))
        if ok and r ~= false then
            if OS.how ~= "json" then OS.how = "json"; C.log("sense.json written with json.dump_file (fallback)") end
            return true
        end
    end
    C.once("nowrite", "cannot write " .. O.PATH .. " (fs.write and json.dump_file both failed)")
    return false
end

function O.publish(st, now)
    now = now or C.clock()
    if OS.t0 == nil then OS.t0 = now end
    if now - OS.last < O.MIN_GAP - 0.005 and now >= OS.last then return false end
    OS.last = now
    local s = {}
    for k, v in pairs(st or {}) do s[k] = v end
    OS.seq = OS.seq + 1
    s.seq = OS.seq
    s.t = math.floor((now - OS.t0) * 1000 + 0.5)
    local okB, msg = pcall(O.build, s)
    if not okB then C.err("out.build", msg); return false end
    local okE, text = pcall(O.encode, msg)
    if not okE then C.err("out.encode", text); return false end
    OS.text = text
    return writeText(msg, text)
end

function O.lastText() return OS.text end

function O.reset()
    OS.last, OS.seq, OS.t0, OS.text, OS.how = -huge, 0, nil, nil, nil
end

-- loader hook (added by the RE integration step): publishes what the other modules left in ctx.
function O.tick(ctx)
    if type(ctx) ~= "table" then return false end
    local P = type(ctx.profile) == "table" and ctx.profile or {}
    local disc = type(P.discovery) == "table" and P.discovery or {}
    return O.publish({
        game = ctx.game, player = ctx.player, foot_dz = ctx.foot_dz, cam = ctx.cam, fov_axis = P.fov_axis,
        screen = ctx.screen, range = C.num(P.range) or C.num(disc.range) or O.RANGE, items = ctx.items,
        classes = ctx.classes, target = ctx.target, learned = ctx.learned, err = ctx.err,
        debug_state = P.debug_state == true,
    }, ctx.now)
end

return O
