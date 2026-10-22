-- Claude AI Bridge for The Binding of Isaac: Repentance / Repentance+
-- Requires launching the game with --luadebug (gives Lua access to io/os).
-- WARNING: --luadebug lets ALL enabled mods touch your files. Only enable mods you trust.
--
-- How it works:
--   Every game update (30/s) the mod writes the current state to STATE_PATH.
--   When its input queue is empty it BLOCKS the game thread (whole game frozen)
--   until the agent writes a new action list to ACT_PATH. Then it plays those
--   inputs one update at a time and freezes again when they run out.

local mod = RegisterMod("Claude AI Bridge", 1)
local json = require("json")

-- Change this folder if you like; must match BRIDGE_DIR in agent.py. Use forward slashes.
local BRIDGE_DIR = "C:/isaac_ai/"
local STATE_PATH = BRIDGE_DIR .. "state.json"
local STATE_TMP  = BRIDGE_DIR .. "state.tmp"
local ACT_PATH   = BRIDGE_DIR .. "actions.json"

local queue = {}      -- per-update input frames: {move="left", shoot="up", bomb=false, item=false}
local current = nil   -- input applied during this update
local seq = 0
local enabled = true

-- Drop leftovers from a previous session so we never replay stale inputs.
os.remove(ACT_PATH)
os.remove(STATE_PATH)

-- New run (or continue): reset the counter and any queued inputs.
mod:AddCallback(ModCallbacks.MC_POST_GAME_STARTED, function()
  queue, current, seq, enabled = {}, nil, 0, true
end)

---------------------------------------------------------------------------
-- File helpers
---------------------------------------------------------------------------
-- On Windows a file that another process is reading cannot be replaced, so the
-- swap can fail if agent.py is reading state.json at that instant. Retry until it
-- works; a silently failed swap would deadlock (game waits, agent never sees state).
local function write_state(tbl)
  local data = json.encode(tbl)
  local deadline = os.clock() + 5
  repeat
    local f = io.open(STATE_TMP, "w")
    if f then
      f:write(data)
      f:close()
      os.remove(STATE_PATH)
      if os.rename(STATE_TMP, STATE_PATH) then return true end
    end
  until os.clock() > deadline
  Isaac.DebugString("[Claude AI Bridge] could not write " .. STATE_PATH)
  return false
end

-- Busy-waits until the agent writes actions. The game is frozen meanwhile.
local function wait_for_actions()
  while true do
    local f = io.open(ACT_PATH, "r")
    if f then
      local s = f:read("*a")
      f:close()
      if s and #s > 0 then
        os.remove(ACT_PATH)
        local ok, data = pcall(json.decode, s)
        if ok and type(data) == "table" then return data end
      end
    end
  end
end

---------------------------------------------------------------------------
-- State export
---------------------------------------------------------------------------
local function v(vec) return { math.floor(vec.X + 0.5), math.floor(vec.Y + 0.5) } end

-- Item names in Repentance are often localization keys like "#SAD_ONION_NAME".
-- Turn those into readable English ("SAD ONION"); keep plain names unchanged.
local function clean(s)
  if type(s) ~= "string" or s == "" then return nil end
  if s:sub(1, 1) ~= "#" then return s end
  s = s:sub(2):gsub("_NAME$", ""):gsub("_DESCRIPTION$", ""):gsub("_DESC$", "")
  return (s:gsub("_", " "))
end

local item_config = Isaac.GetItemConfig()

local function item_info(id)
  local c = id and id > 0 and item_config:GetCollectible(id)
  if not c then return nil end
  return {
    id = id,
    name = clean(c.Name),
    desc = clean(c.Description),
    quality = c.Quality,                                   -- 0 (junk) .. 4 (best)
    active = c.Type == ItemType.ITEM_ACTIVE,
    max_charge = c.MaxCharges,
  }
end

local function trinket_info(id)
  local c = id and id > 0 and item_config:GetTrinket(id)
  if not c then return nil end
  return { id = id, name = clean(c.Name), desc = clean(c.Description) }
end

local PICKUP_KIND = {
  [PickupVariant.PICKUP_HEART] = "heart", [PickupVariant.PICKUP_COIN] = "coin",
  [PickupVariant.PICKUP_KEY] = "key", [PickupVariant.PICKUP_BOMB] = "bomb",
  [PickupVariant.PICKUP_CHEST] = "chest", [PickupVariant.PICKUP_LOCKEDCHEST] = "locked_chest",
  [PickupVariant.PICKUP_PILL] = "pill", [PickupVariant.PICKUP_LIL_BATTERY] = "battery",
  [PickupVariant.PICKUP_TAROTCARD] = "card", [PickupVariant.PICKUP_TRINKET] = "trinket",
  [PickupVariant.PICKUP_COLLECTIBLE] = "item",
}

-- Price: >0 coins; <0 is a heart deal (-1 one red heart, -2 two red, -3 three soul, ...).
local function describe_pickup(p)
  local out = { kind = PICKUP_KIND[p.Variant] or ("variant_" .. p.Variant), sub = p.SubType,
                pos = v(p.Position), price = p.Price }
  if p.OptionsPickupIndex and p.OptionsPickupIndex ~= 0 then
    out.choice_group = p.OptionsPickupIndex   -- taking one removes the others in the group
  end
  if p.Variant == PickupVariant.PICKUP_COLLECTIBLE then
    if p.SubType == 0 then out.kind = "empty_pedestal"
    else out.item = item_info(p.SubType) end
  elseif p.Variant == PickupVariant.PICKUP_TRINKET then
    out.trinket = trinket_info(p.SubType)
  end
  return out
end

local ROOM_NAME = {
  [1] = "normal", [2] = "shop", [3] = "error", [4] = "treasure", [5] = "boss",
  [6] = "miniboss", [7] = "secret", [8] = "super_secret", [9] = "arcade", [10] = "curse",
  [11] = "challenge", [12] = "library", [13] = "sacrifice", [14] = "devil", [15] = "angel",
  [16] = "crawlspace", [17] = "boss_rush", [18] = "clean_bedroom", [19] = "dirty_bedroom",
  [20] = "vault", [21] = "dice", [22] = "black_market", [23] = "greed_exit",
  [24] = "planetarium", [25] = "teleporter", [26] = "teleporter_exit", [27] = "secret_exit",
  [28] = "blue", [29] = "ultra_secret",
}
local DOOR_DIR = { [0] = "left", [1] = "up", [2] = "right", [3] = "down",
                   [4] = "left2", [5] = "up2", [6] = "right2", [7] = "down2" }

-- Floor map, limited to rooms the player could see on the in-game minimap
-- (visited, revealed by map items, or shown as an icon). No cheating.
local function floor_map(level)
  local rooms = {}
  local list = level:GetRooms()
  for i = 0, list.Size - 1 do
    local d = list:Get(i)
    if d and d.Data and (d.VisitedCount > 0 or d.DisplayFlags ~= 0) then
      rooms[#rooms + 1] = {
        x = d.GridIndex % 13, y = math.floor(d.GridIndex / 13),
        type = ROOM_NAME[d.Data.Type] or tostring(d.Data.Type),
        visited = d.VisitedCount > 0, clear = d.Clear,
      }
    end
  end
  local cur = level:GetCurrentRoomDesc()
  local here = cur and cur.GridIndex >= 0 and { x = cur.GridIndex % 13, y = math.floor(cur.GridIndex / 13) } or nil
  return { you_are_at = here, rooms = rooms, note = "x grows right, y grows down; adjacent rooms differ by 1" }
end

-- Passive + active items the player currently owns.
local function inventory(player)
  local owned = {}
  local ok, list = pcall(function() return item_config:GetCollectibles() end)
  local max_id = (ok and list and list.Size) or 1000
  for id = 1, max_id do
    if item_config:GetCollectible(id) and player:HasCollectible(id) then
      local info = item_info(id)
      info.count = player:GetCollectibleNum(id)
      owned[#owned + 1] = info
    end
  end
  return owned
end

local function collect_state()
  local game = Game()
  local room = game:GetRoom()
  local level = game:GetLevel()
  local player = Isaac.GetPlayer(0)

  local enemies, projectiles, pickups = {}, {}, {}
  for _, e in ipairs(Isaac.GetRoomEntities()) do
    if e:IsActiveEnemy(false) and e:IsVulnerableEnemy() then
      enemies[#enemies + 1] = { t = e.Type, var = e.Variant, pos = v(e.Position),
                                vel = v(e.Velocity), hp = math.floor(e.HitPoints), r = math.floor(e.Size) }
    elseif e.Type == EntityType.ENTITY_PROJECTILE then
      projectiles[#projectiles + 1] = { pos = v(e.Position), vel = v(e.Velocity) }
    elseif e.Type == EntityType.ENTITY_PICKUP then
      local p = e:ToPickup()
      if p then pickups[#pickups + 1] = describe_pickup(p) end
    end
  end

  local doors = {}
  for slot = 0, DoorSlot.NUM_DOOR_SLOTS - 1 do
    local d = room:GetDoor(slot)
    if d then
      doors[#doors + 1] = { dir = DOOR_DIR[slot] or tostring(slot), pos = v(room:GetDoorSlotPosition(slot)),
                            open = d:IsOpen(), leads_to = ROOM_NAME[d.TargetRoomType] or tostring(d.TargetRoomType) }
    end
  end

  local obstacles = {}
  for i = 0, room:GetGridSize() - 1 do
    local g = room:GetGridEntity(i)
    if g and g.CollisionClass ~= GridCollisionClass.COLLISION_NONE then
      obstacles[#obstacles + 1] = { pos = v(room:GetGridPosition(i)), type = g:GetType() }
    end
  end

  return {
    seq = seq,
    frame = game:GetFrameCount(),
    stage = level:GetStage(),
    room_index = level:GetCurrentRoomIndex(),
    room_type = ROOM_NAME[room:GetType()] or tostring(room:GetType()),
    map = floor_map(level),
    room_clear = room:IsClear(),
    bounds = { v(room:GetTopLeftPos()), v(room:GetBottomRightPos()) },
    player = {
      pos = v(player.Position), vel = v(player.Velocity),
      red = player:GetHearts(), max_red = player:GetMaxHearts(), soul = player:GetSoulHearts(),
      bombs = player:GetNumBombs(), keys = player:GetNumKeys(), coins = player:GetNumCoins(),
      dead = player:IsDead(),
      stats = { damage = player.Damage, tears_delay = player.MaxFireDelay, speed = player.MoveSpeed,
                range = player.TearRange, shot_speed = player.ShotSpeed, luck = player.Luck,
                can_fly = player.CanFly },
      active = player:GetActiveItem(ActiveSlot.SLOT_PRIMARY) ~= 0 and {
        item = item_info(player:GetActiveItem(ActiveSlot.SLOT_PRIMARY)),
        charge = player:GetActiveCharge(ActiveSlot.SLOT_PRIMARY),
      } or nil,
      trinket = trinket_info(player:GetTrinket(0)),
      card = player:GetCard(0) ~= 0 and player:GetCard(0) or nil,
      pill = player:GetPill(0) ~= 0 and player:GetPill(0) or nil,
      items = inventory(player),
    },
    enemies = enemies, projectiles = projectiles, pickups = pickups,
    doors = doors, obstacles = obstacles,
  }
end

---------------------------------------------------------------------------
-- Main loop: one call per game update
---------------------------------------------------------------------------
mod:AddCallback(ModCallbacks.MC_POST_UPDATE, function()
  if not enabled then return end
  seq = seq + 1

  if #queue == 0 then
    -- Never let a Lua error silently stop the bridge: report it to agent.py instead.
    local ok, st = pcall(collect_state)
    if not ok then
      st = { seq = seq, lua_error = tostring(st), player = { dead = false } }
      Isaac.DebugString("[Claude AI Bridge] " .. tostring(st.lua_error))
    end
    write_state(st)
    local data = wait_for_actions()          -- game frozen here
    if data.disable then enabled = false; current = nil; return end
    for _, a in ipairs(data.frames or {}) do queue[#queue + 1] = a end
  end

  current = table.remove(queue, 1)
end)

---------------------------------------------------------------------------
-- Input override
---------------------------------------------------------------------------
local MOVE = {
  left  = { ButtonAction.ACTION_LEFT },  right = { ButtonAction.ACTION_RIGHT },
  up    = { ButtonAction.ACTION_UP },    down  = { ButtonAction.ACTION_DOWN },
  upleft = { ButtonAction.ACTION_UP, ButtonAction.ACTION_LEFT },
  upright = { ButtonAction.ACTION_UP, ButtonAction.ACTION_RIGHT },
  downleft = { ButtonAction.ACTION_DOWN, ButtonAction.ACTION_LEFT },
  downright = { ButtonAction.ACTION_DOWN, ButtonAction.ACTION_RIGHT },
}
local SHOOT = {
  left = ButtonAction.ACTION_SHOOTLEFT, right = ButtonAction.ACTION_SHOOTRIGHT,
  up = ButtonAction.ACTION_SHOOTUP, down = ButtonAction.ACTION_SHOOTDOWN,
}
local CONTROLLED = {
  [ButtonAction.ACTION_LEFT] = true, [ButtonAction.ACTION_RIGHT] = true,
  [ButtonAction.ACTION_UP] = true, [ButtonAction.ACTION_DOWN] = true,
  [ButtonAction.ACTION_SHOOTLEFT] = true, [ButtonAction.ACTION_SHOOTRIGHT] = true,
  [ButtonAction.ACTION_SHOOTUP] = true, [ButtonAction.ACTION_SHOOTDOWN] = true,
  [ButtonAction.ACTION_BOMB] = true, [ButtonAction.ACTION_ITEM] = true,
  [ButtonAction.ACTION_PILLCARD] = true,
}

local function is_held(action)
  if not current then return false end
  for _, a in ipairs(MOVE[current.move] or {}) do if a == action then return true end end
  if SHOOT[current.shoot] == action then return true end
  if action == ButtonAction.ACTION_BOMB and current.bomb then return true end
  if action == ButtonAction.ACTION_ITEM and current.item then return true end
  if action == ButtonAction.ACTION_PILLCARD and current.pillcard then return true end
  return false
end

mod:AddCallback(ModCallbacks.MC_INPUT_ACTION, function(_, entity, hook, action)
  if not enabled or not CONTROLLED[action] then return nil end   -- menus etc. stay manual
  if entity and entity.Type ~= EntityType.ENTITY_PLAYER then return nil end
  local held = is_held(action)
  if hook == InputHook.GET_ACTION_VALUE then return held and 1.0 or 0.0 end
  return held
end)
