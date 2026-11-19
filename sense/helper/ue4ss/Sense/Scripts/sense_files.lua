-- sense_files: file helpers for the Sense helper (pure Lua, never raises).
-- Usage: local files = require("sense_files")
--   files.modDir()               -> dir, how     the mod folder (...\Mods\Sense), how it was found
--   files.fileExists(path)       -> true|false
--   files.writeAtomic(path, text)-> true | false, err, busy   (writes path.tmp, then swaps it in)
--   files.readAll(path)          -> text | nil, err
--   files.loadJson(path)         -> tbl | nil, err   (missing file, broken JSON, or not an object/array -> nil, err)
--   files.saveJson(path, tbl)    -> true | false, err, busy  (json.encode + writeAtomic)
-- The folder of path must already exist (pure Lua cannot make folders without starting a console program).

local json = require("sense_json")

local files = {}

local SWAP_TRIES  = 2       -- tries to swap path.tmp in (the reader may hold the file open for a moment)
local SWAP_WAIT_S = 0.001   -- wait between those tries (seconds)

local function spinWait(s)   -- short busy wait (Lua has no sleep); only used for SWAP_WAIT_S
    local t = os.clock()
    while os.clock() - t < s do end
end

function files.fileExists(path)
    local ok, res = pcall(function()
        local f = io.open(path, "rb")
        if f then f:close(); return true end
        return false
    end)
    return ok and res == true
end
local fileExists = files.fileExists

-- write the whole text to path.tmp, then swap it in; the reader never sees a half file.
-- Returns true, or false + text + busy. busy = true means the old file is still there and whole: the reader had it
-- open at that moment (on Windows os.remove then fails), so the swap is simply skipped. That is not an error:
-- the next write tries again. busy = false means something really went wrong. That includes the case where the
-- old file WAS removed but the tmp could not be renamed (something else holds the tmp).
local function writeAtomicRaw(path, text)
    local tmp = path .. ".tmp"
    local f, err = io.open(tmp, "wb")
    if not f then return false, "cannot open " .. tmp .. " (" .. tostring(err) .. ")", false end
    local okw, errw = f:write(text)
    f:close()
    if not okw then return false, "cannot write " .. tmp .. " (" .. tostring(errw) .. ")", false end
    local errr = nil
    local removed = false   -- true once the old file is gone (removed now, or it was never there)
    for try = 1, SWAP_TRIES do
        if not removed then
            local okd, ed = os.remove(path)
            if okd or not fileExists(path) then
                removed = true        -- gone now, or there was no old file to begin with
            else
                errr = ed             -- reader holding it open: the old file stays whole, try again shortly
            end
        end
        if removed then
            local okr, e = os.rename(tmp, path)
            if okr then return true end
            errr = e
        end
        if try < SWAP_TRIES then spinWait(SWAP_WAIT_S) end
    end
    if removed then
        return false, "cannot rename " .. tmp .. " to " .. path .. " (old file is gone) (" .. tostring(errr) .. ")", false
    end
    return false, "file busy, kept the old " .. path .. " (" .. tostring(errr) .. ")", true
end

function files.writeAtomic(path, text)
    if type(path) ~= "string" or path == "" then return false, "no path", false end
    if type(text) ~= "string" then return false, "text is not a string", false end
    local ok, a, b, c = pcall(writeAtomicRaw, path, text)
    if ok then return a, b, c end
    return false, "write failed (" .. tostring(a) .. ")", false
end

function files.readAll(path)
    if type(path) ~= "string" or path == "" then return nil, "no path" end
    local ok, text, err = pcall(function()
        local f, e = io.open(path, "rb")
        if not f then return nil, "cannot open " .. path .. " (" .. tostring(e) .. ")" end
        local s = f:read("a")
        f:close()
        if s == nil then return nil, "cannot read " .. path end
        return s
    end)
    if not ok then return nil, "read failed (" .. tostring(text) .. ")" end
    return text, err
end

function files.loadJson(path)
    local text, err = files.readAll(path)
    if not text then return nil, err end
    local v, derr = json.decode(text)
    if v == nil then return nil, "broken JSON in " .. tostring(path) .. ": " .. tostring(derr) end
    if type(v) ~= "table" or v == json.NULL then return nil, "JSON in " .. tostring(path) .. " is not an object" end
    return v
end

function files.saveJson(path, tbl)
    if type(tbl) ~= "table" then return false, "nothing to save (not a table)", false end
    local text = json.encode(tbl)
    return files.writeAtomic(path, text)
end

-- ---------- where this mod lives ----------
function files.modDir()
    local ok, dir, how = pcall(function()
        local dbg = rawget(_G, "debug")
        local okg, info = pcall(function() return dbg.getinfo(1, "S") end)   -- "@<game folder>\...\Mods\Sense\Scripts\sense_files.lua"
        local src = okg and info and info.source or ""
        if src:sub(1, 1) == "@" then src = src:sub(2) end
        local d = src:match("^(.+)[/\\]Scripts[/\\][^/\\]+$")
        if d then return d, "script path" end
        local pp = type(package) == "table" and package.path or ""         -- UE4SS puts <Mods>\shared on package.path
        local mods = pp:match("([^;]-[/\\]Mods)[/\\]shared[/\\]")
        if mods then return mods .. "\\Sense", "package.path" end
        return "ue4ss\\Mods\\Sense", "relative guess"                     -- the game's working folder is normally next to the exe
    end)
    if ok and type(dir) == "string" then return dir, how end
    return "ue4ss\\Mods\\Sense", "relative guess"
end

return files
