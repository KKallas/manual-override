(function (global) {
  "use strict";

  const WIDTH = 1696;
  const HEIGHT = 960;
  const LIVE_POD_SIZE = 112;
  const TOWER_VISUAL_SIZE = 88;
  const TOWER_HEALTH_BAR_WIDTH = 68;
  const TOWER_DAMAGE_FLASH_S = 0.45;
  const ARUCO_FIELD_CLEARANCE = 20;
  const CORE_MARKER_VISUAL_SIZE = 116;
  const FORCE_FIELD_ZAP_DURATION_S = 0.5;
  const MACHINE_GUN_MUZZLE_FORWARD = 36;
  const MACHINE_GUN_MUZZLE_HALF_GAP = 7;
  const FLAMETHROWER_PATH_SEGMENTS = 18;
  const FLAMETHROWER_TRAIL_LAG_S = 0.48;
  const FLAMETHROWER_MUZZLE_OFFSET = 35;
  const FLAMETHROWER_PILOT_LAG_S = 0.08;
  const TESLA_DISCHARGE_FLASH_S = 0.16;

  function fixedMarkerVisualSize(width, height = width) {
    return Math.max(42, Math.min(104, Math.round(Math.min(width, height) * 0.37)));
  }

  function machineGunMuzzlePoints(x, y, angle) {
    const forwardX = Math.cos(angle);
    const forwardY = Math.sin(angle);
    const normalX = -forwardY;
    const normalY = forwardX;
    const centerX = Number(x) + forwardX * MACHINE_GUN_MUZZLE_FORWARD;
    const centerY = Number(y) + forwardY * MACHINE_GUN_MUZZLE_FORWARD;
    return [-1, 1].map((side) => ({
      x: centerX + normalX * MACHINE_GUN_MUZZLE_HALF_GAP * side,
      y: centerY + normalY * MACHINE_GUN_MUZZLE_HALF_GAP * side,
    }));
  }

  function machineGunFireLines(x, y, angle, targetX, targetY) {
    return machineGunMuzzlePoints(x, y, angle).map((muzzle) => ({
      ax: muzzle.x,
      ay: muzzle.y,
      bx: Number(targetX),
      by: Number(targetY),
    }));
  }

  function flamethrowerNozzlePoint(x, y, angle) {
    return {
      x: Number(x) + Math.cos(angle) * FLAMETHROWER_MUZZLE_OFFSET,
      y: Number(y) + Math.sin(angle) * FLAMETHROWER_MUZZLE_OFFSET,
    };
  }

  function advancedWeaponCharge(snapshotCharge, chargeDuration, elapsed) {
    const initial = Math.max(0, Math.min(1, Number(snapshotCharge) || 0));
    const duration = Math.max(0.001, Number(chargeDuration) || 1);
    return Math.max(0, Math.min(1, initial + Math.max(0, Number(elapsed) || 0) / duration));
  }

  function towerLinkMultiplierLabel(tower) {
    const linkedTurretCount = Math.max(1, Math.round(Number(tower.linked_turret_count) || 1));
    const snapshotMultiplier = Number(tower.link_multiplier);
    const linkMultiplier = Number.isFinite(snapshotMultiplier)
      ? snapshotMultiplier
      : 0.9 + (linkedTurretCount - 1) * 0.1;
    return {
      linkedTurretCount,
      linkMultiplier,
      label: `×${linkMultiplier.toFixed(2).replace(/0$/, "")}`,
    };
  }

  function towerHealthBarMetrics(tower, visualTime) {
    const maximum = Math.max(1, Number(tower.max_hp || 1));
    const healthRatio = Math.max(0, Math.min(1, Number(tower.hp || 0) / maximum));
    const damagedAt = tower.last_damage_at == null ? NaN : Number(tower.last_damage_at);
    const damageAge = Number(visualTime) - damagedAt;
    const damageAlpha = Number.isFinite(damageAge) && damageAge >= 0 && damageAge <= TOWER_DAMAGE_FLASH_S
      ? 1 - damageAge / TOWER_DAMAGE_FLASH_S
      : 0;
    const damageAmount = Math.max(0, Number(tower.last_damage_amount || 0));
    return {
      healthRatio,
      fillWidth: TOWER_HEALTH_BAR_WIDTH * healthRatio,
      damageAlpha,
      damageNotchWidth: damageAlpha > 0 && damageAmount > 0
        ? Math.max(1, Math.min(TOWER_HEALTH_BAR_WIDTH, TOWER_HEALTH_BAR_WIDTH * damageAmount / maximum))
        : 0,
    };
  }

  function normalizedKeepOut(keepOut, padding = 0) {
    const left = Number.isFinite(Number(keepOut.left))
      ? Number(keepOut.left)
      : Number(keepOut.x) - Number(keepOut.halfWidth ?? keepOut.halfSize ?? 0);
    const right = Number.isFinite(Number(keepOut.right))
      ? Number(keepOut.right)
      : Number(keepOut.x) + Number(keepOut.halfWidth ?? keepOut.halfSize ?? 0);
    const top = Number.isFinite(Number(keepOut.top))
      ? Number(keepOut.top)
      : Number(keepOut.y) - Number(keepOut.halfHeight ?? keepOut.halfSize ?? 0);
    const bottom = Number.isFinite(Number(keepOut.bottom))
      ? Number(keepOut.bottom)
      : Number(keepOut.y) + Number(keepOut.halfHeight ?? keepOut.halfSize ?? 0);
    if (![left, right, top, bottom].every(Number.isFinite)) return null;
    return {
      markerId: Number(keepOut.markerId ?? keepOut.marker_id),
      left: Math.min(left, right) - padding,
      right: Math.max(left, right) + padding,
      top: Math.min(top, bottom) - padding,
      bottom: Math.max(top, bottom) + padding,
    };
  }

  function segmentRectangleInterval(ax, ay, bx, by, rectangle) {
    const dx = bx - ax;
    const dy = by - ay;
    let enter = 0;
    let exit = 1;
    const boundaries = [
      [-dx, ax - rectangle.left],
      [dx, rectangle.right - ax],
      [-dy, ay - rectangle.top],
      [dy, rectangle.bottom - ay],
    ];
    for (const [direction, distance] of boundaries) {
      if (Math.abs(direction) <= 1e-9) {
        if (distance < 0) return null;
        continue;
      }
      const ratio = distance / direction;
      if (direction < 0) enter = Math.max(enter, ratio);
      else exit = Math.min(exit, ratio);
      if (enter > exit) return null;
    }
    return [Math.max(0, enter), Math.min(1, exit)];
  }

  function fieldSegmentsOutsideKeepOuts(ax, ay, bx, by, keepOuts) {
    let visibleIntervals = [[0, 1]];
    for (const keepOut of keepOuts) {
      const blocked = segmentRectangleInterval(ax, ay, bx, by, keepOut);
      if (!blocked || blocked[1] - blocked[0] <= 1e-9) continue;
      const nextIntervals = [];
      for (const [start, end] of visibleIntervals) {
        if (blocked[1] <= start || blocked[0] >= end) {
          nextIntervals.push([start, end]);
          continue;
        }
        if (blocked[0] > start + 1e-9) {
          nextIntervals.push([start, Math.min(end, blocked[0])]);
        }
        if (blocked[1] < end - 1e-9) {
          nextIntervals.push([Math.max(start, blocked[1]), end]);
        }
      }
      visibleIntervals = nextIntervals;
      if (!visibleIntervals.length) break;
    }
    return visibleIntervals.map(([start, end]) => ({
      start,
      end,
      ax: ax + (bx - ax) * start,
      ay: ay + (by - ay) * start,
      bx: ax + (bx - ax) * end,
      by: ay + (by - ay) * end,
    }));
  }

  function circleOverlapsKeepOut(x, y, radius, keepOut) {
    const nearestX = Math.max(keepOut.left, Math.min(x, keepOut.right));
    const nearestY = Math.max(keepOut.top, Math.min(y, keepOut.bottom));
    return Math.hypot(x - nearestX, y - nearestY) <= radius;
  }

  function propertyMap(object) {
    return Object.fromEntries((object.properties || []).map((item) => [item.name, item.value]));
  }

  function createTowerDefenceView(options) {
    const root = String(options.root || "").replace(/\/$/, "");
    const mapCanvas = options.mapCanvas;
    const gameCanvas = options.gameCanvas;
    const fieldCanvas = options.fieldCanvas || null;
    if (!root || !mapCanvas || !gameCanvas) {
      throw new Error("Tower Defense view requires root, mapCanvas, and gameCanvas");
    }

    const fetchJson = options.fetchJson || (async (url) => {
      const response = await fetch(url);
      const text = await response.text();
      let data = {};
      try {
        data = text ? JSON.parse(text) : {};
      } catch (_) {
        // The response status below supplies the useful failure when JSON is invalid.
      }
      if (!response.ok) throw new Error(data.error || text || `HTTP ${response.status}`);
      return data;
    });
    const images = new Map();
    const gameImages = new Map();
    const tintedEffectCache = new Map();
    const enemySpriteCache = new Map();
    const enemyTrailHistory = new Map();
    const towerRenderAngles = new Map();
    const markerImages = new Map();
    let level = null;
    let tilesets = [];
    let state = null;
    let stateReceivedAt = performance.now();
    let lastGameRenderAt = 0;
    let animationFrame = 0;
    let destroyed = false;
    let layoutEditing = false;
    let selectedSocketId = null;
    let selectedTowerId = null;
    let detectedMarkerKeepOuts = [];
    const towerAimPreview = new Map();
    let mapRenderQueued = false;

    mapCanvas.width = WIDTH;
    mapCanvas.height = HEIGHT;
    gameCanvas.width = WIDTH;
    gameCanvas.height = HEIGHT;
    if (fieldCanvas) {
      fieldCanvas.width = WIDTH;
      fieldCanvas.height = HEIGHT;
    }

    function urlFrom(relative, base) {
      return new URL(relative, global.location.origin + base).pathname;
    }

    function loadImage(url) {
      if (images.has(url)) return images.get(url);
      const promise = new Promise((resolve, reject) => {
        const image = new Image();
        image.onload = () => resolve(image);
        image.onerror = () => reject(new Error(`asset failed: ${url}`));
        image.src = url;
      });
      images.set(url, promise);
      return promise;
    }

    function towerRuntimeImagePath(type, layer) {
      const version = type === "tesla_coil" && layer === "head" ? 2 : 1;
      return `${root}/assets/game-art/z-pixel-v2/normalized/structures/runtime/${type.replace("_", "-")}-${layer}-v${version}.png`;
    }

    function enemyImagePath(type, frame) {
      const group = type === "brute" ? "enemies-heavy-orcs-v2" : "enemies-light-orcs-v2";
      return `${root}/assets/game-art/sprites/${group}/${type}-walk-${String(frame).padStart(2, "0")}.png`;
    }

    function combatEffectPath(name) {
      const version = name === "flame-gasoline" ? "v3" : "v1";
      return `${root}/assets/game-art/z-pixel-v2/normalized/effects/combat/${name}-${version}.png`;
    }

    async function loadGameImages() {
      const pending = [];
      for (const type of ["machine_gun", "flamethrower", "mortar", "tesla_coil"]) {
        pending.push(loadImage(towerRuntimeImagePath(type, "base")).then((image) => gameImages.set(`tower:${type}:base`, image)));
        pending.push(loadImage(towerRuntimeImagePath(type, "head")).then((image) => gameImages.set(`tower:${type}:head`, image)));
      }
      pending.push(loadImage(`${root}/assets/game-art/z-pixel-v2/normalized/structures/runtime/tower-socket-cover-v1.png`).then((image) => gameImages.set("tower:socket-cover", image)));
      for (const type of ["grunt", "runner", "breaker", "brute"]) {
        for (let frame = 1; frame <= 4; frame += 1) {
          pending.push(loadImage(enemyImagePath(type, frame)).then((image) => {
            gameImages.set(`enemy:${type}:${frame}`, image);
          }));
        }
      }
      for (const effect of ["machine-gun-impact", "machine-gun-bullet", "flame-burn", "flame-gasoline", "mortar-impact", "mortar-shell", "tesla-spark", "tower-smoke", "tower-fire", "tower-stress-cracks", "tower-destruction-blast", "tower-debris", "force-field-impact", "force-field-zap-skeleton", "core-ring-aura", "core-detonation-burst", "core-purge-wave"]) {
        pending.push(loadImage(combatEffectPath(effect)).then((image) => gameImages.set(`effect:${effect}`, image)));
      }
      await Promise.all(pending);
    }

    function tileForGid(gid) {
      let selected = null;
      for (const tileset of tilesets) {
        if (gid >= tileset.firstgid && (!selected || tileset.firstgid > selected.firstgid)) selected = tileset;
      }
      if (!selected) return null;
      const tile = selected.tiles.get(gid - selected.firstgid);
      return tile ? { tile, objectAlignment: selected.source.objectalignment || "bottomleft" } : null;
    }

    function tileDrawOffset(alignment, width, height) {
      const value = String(alignment || "bottomleft").toLowerCase();
      const dx = value.includes("left") ? 0 : value.includes("right") ? -width : -width / 2;
      const dy = value.startsWith("top") ? 0 : value.startsWith("bottom") ? -height : -height / 2;
      return { dx, dy };
    }

    function tileObjectPoint(object, normalizedX = 0.5, normalizedY = 0.5) {
      const resolved = tileForGid(object.gid);
      if (!resolved) return { x: Number(object.x || 0), y: Number(object.y || 0) };
      const width = Number(object.width || resolved.tile.imagewidth);
      const height = Number(object.height || resolved.tile.imageheight);
      const { dx, dy } = tileDrawOffset(resolved.objectAlignment, width, height);
      const localX = dx + width * normalizedX;
      const localY = dy + height * normalizedY;
      const rotation = Number(object.rotation || 0) * Math.PI / 180;
      const cos = Math.cos(rotation);
      const sin = Math.sin(rotation);
      return {
        x: Number(object.x || 0) + localX * cos - localY * sin,
        y: Number(object.y || 0) + localX * sin + localY * cos,
      };
    }

    function tileObjectCenter(object) {
      return tileObjectPoint(object);
    }

    function arucoMarkerCenter(object) {
      const properties = propertyMap(object);
      const anchorU = Number(properties.aruco_anchor_u);
      const anchorV = Number(properties.aruco_anchor_v);
      return tileObjectPoint(
        object,
        Number.isFinite(anchorU) ? anchorU : 0.5,
        Number.isFinite(anchorV) ? anchorV : 0.5,
      );
    }

    function socketMarkerVisualSize() {
      const configured = Number(propertyMap(level || {}).aruco_code_footprint_px);
      return Number.isFinite(configured) ? configured : fixedMarkerVisualSize(208);
    }

    async function drawTile(context, object) {
      const resolved = tileForGid(object.gid);
      if (!resolved) return;
      const image = await loadImage(resolved.tile.imageUrl);
      const width = Number(object.width || resolved.tile.imagewidth);
      const height = Number(object.height || resolved.tile.imageheight);
      const x = Number(object.x || 0);
      const y = Number(object.y || 0);
      const rotation = Number(object.rotation || 0) * Math.PI / 180;
      const { dx, dy } = tileDrawOffset(resolved.objectAlignment, width, height);
      context.save();
      context.translate(x, y);
      context.rotate(rotation);
      context.drawImage(image, dx, dy, width, height);
      context.restore();
    }

    function socketLayer() {
      return (level?.layers || []).find((item) => item.name.includes("Placement Spots"));
    }

    function socketObjects() {
      return socketLayer()?.objects || [];
    }

    function updateGateGeometry() {
      const sockets = new Map(socketObjects().map((object) => [Number(object.id), object]));
      for (const layer of level?.layers || []) {
        for (const object of layer.objects || []) {
          if (!['ForceFieldWall', 'GateHint'].includes(object.type)) continue;
          const properties = propertyMap(object);
          const a = sockets.get(Number(properties.socket_a));
          const b = sockets.get(Number(properties.socket_b));
          if (!a || !b) continue;
          const ax = Number(a.x), ay = Number(a.y), bx = Number(b.x), by = Number(b.y);
          if (object.type === 'GateHint') {
            object.x = ax;
            object.y = ay;
            object.polyline = [{ x: 0, y: 0 }, { x: bx - ax, y: by - ay }];
            continue;
          }
          const dx = bx - ax, dy = by - ay;
          object.x = (ax + bx) / 2;
          object.y = (ay + by) / 2;
          object.height = Math.max(32, Math.hypot(dx, dy) + 14);
          object.rotation = Math.atan2(dy, dx) * 180 / Math.PI - 90;
        }
      }
    }

    function scheduleMapRender() {
      if (mapRenderQueued || destroyed) return;
      mapRenderQueued = true;
      global.requestAnimationFrame(() => {
        mapRenderQueued = false;
        renderMap().catch(() => {});
      });
    }

    async function renderMap() {
      if (!level) return;
      const context = mapCanvas.getContext("2d");
      context.clearRect(0, 0, WIDTH, HEIGHT);
      for (const layer of level.layers || []) {
        if (layer.visible === false) continue;
        context.save();
        context.globalAlpha = Number(layer.opacity ?? 1);
        for (const object of layer.objects || []) {
          if (object.visible === false) continue;
          if (object.gid) {
            await drawTile(context, object);
            continue;
          }
          if (object.type === "ActivationStagingZone") {
            context.fillStyle = "#ff9f4326";
            context.strokeStyle = "#ff9f43";
            context.lineWidth = 3;
            context.fillRect(object.x, object.y, object.width, object.height);
            context.strokeRect(object.x, object.y, object.width, object.height);
          }
          if (object.type === "ActivatorStart") {
            const properties = propertyMap(object);
            context.fillStyle = properties.owner === "green" ? "#35d07f" : "#c084fc";
            context.beginPath();
            context.arc(object.x, object.y, 15, 0, Math.PI * 2);
            context.fill();
            context.fillStyle = "#061018";
            context.font = "900 13px ui-monospace";
            context.textAlign = "center";
            context.fillText(String(properties.atom_tag_id), object.x, object.y + 5);
          }
        }
        context.restore();
      }
      const sockets = socketLayer();
      if (!sockets) return;
      for (const socket of sockets.objects || []) {
        const properties = propertyMap(socket);
        const center = arucoMarkerCenter(socket);
        const marker = markerImages.get(Number(properties.aruco_id));
        if (!marker) continue;
        const markerSize = socketMarkerVisualSize();
        context.imageSmoothingEnabled = false;
        context.drawImage(
          marker,
          Math.round(center.x - markerSize / 2),
          Math.round(center.y - markerSize / 2),
          markerSize,
          markerSize,
        );
      }
      const coreMarker = markerImages.get(38);
      if (coreMarker) {
        const markerRecord = coreMarkerRecord();
        context.imageSmoothingEnabled = false;
        context.drawImage(
          coreMarker,
          Math.round(markerRecord.x - markerRecord.size / 2),
          Math.round(markerRecord.y - markerRecord.size / 2),
          markerRecord.size,
          markerRecord.size,
        );
      }
    }

    function visualSimulationTime(now, gameState) {
      const serverTime = Number(gameState.sim_time || 0);
      const advancing = gameState.phase === "running" && !gameState.paused;
      return serverTime + (advancing ? Math.max(0, now - stateReceivedAt) / 1000 : 0);
    }

    function centralCoreObject() {
      const layer = (level?.layers || []).find((item) => item.name.includes("Central Square Core"));
      return (layer?.objects || []).find((item) => item.name === "central_core_square_base") || null;
    }

    function centralCoreCenter() {
      const object = centralCoreObject();
      return object ? tileObjectCenter(object) : { x: 880, y: 480 };
    }

    function coreMarkerRecord() {
      const object = centralCoreObject();
      const center = object ? arucoMarkerCenter(object) : centralCoreCenter();
      const configured = Number(propertyMap(level || {}).core_aruco_code_footprint_px);
      return {
        x: center.x,
        y: center.y,
        size: Number.isFinite(configured) ? configured : CORE_MARKER_VISUAL_SIZE,
      };
    }

    function renderCoreMarkerOverlay(context, gameState) {
      const stage = String(gameState.core_sequence?.stage || "locked");
      const marker = markerImages.get(38);
      if (!marker || !["first_tag", "ring_ready"].includes(stage)) return;
      const record = coreMarkerRecord();
      context.save();
      context.imageSmoothingEnabled = false;
      context.drawImage(
        marker,
        Math.round(record.x - record.size / 2),
        Math.round(record.y - record.size / 2),
        record.size,
        record.size,
      );
      context.restore();
    }

    function renderCoreHealth(context, gameState) {
      const center = centralCoreCenter();
      const ratio = Math.max(0, Math.min(1, Number(gameState.core_hp) / Math.max(1, Number(gameState.core_max_hp))));
      const percent = Math.round(ratio * 100);
      const width = 144;
      const height = 15;
      const x = center.x - width / 2;
      const y = center.y + 54;
      const color = ratio > 0.5 ? "#35d07f" : ratio > 0.2 ? "#ffb347" : "#ff5367";
      context.save();
      context.shadowColor = "#000";
      context.shadowBlur = 6;
      context.fillStyle = "#05080de8";
      context.fillRect(x - 3, y - 3, width + 6, height + 6);
      context.fillStyle = "#301017";
      context.fillRect(x, y, width, height);
      context.fillStyle = color;
      context.fillRect(x, y, width * ratio, height);
      context.strokeStyle = "#dceaff";
      context.lineWidth = 2;
      context.strokeRect(x, y, width, height);
      context.shadowBlur = 0;
      context.fillStyle = "#fff";
      context.font = "900 11px ui-monospace,monospace";
      context.textAlign = "center";
      context.textBaseline = "middle";
      context.fillText(`CORE ${percent}%`, center.x, y + height / 2 + 0.5);
      context.restore();
    }

    function renderLayoutEditor(context) {
      if (!layoutEditing) return;
      const records = socketRecords();
      context.save();
      context.textAlign = "center";
      context.textBaseline = "middle";
      for (const socket of records) {
        const selected = socket.socket_id === selectedSocketId;
        const half = socket.size / 2;
        context.strokeStyle = selected ? "#36dfff" : "#ffffffa6";
        context.lineWidth = selected ? 4 : 2;
        context.setLineDash(selected ? [] : [9, 7]);
        context.strokeRect(socket.x - half, socket.y - half, socket.size, socket.size);
        if (!selected) continue;
        context.setLineDash([]);
        context.fillStyle = "#36dfff";
        context.strokeStyle = "#00131a";
        context.lineWidth = 3;
        context.fillRect(socket.x + half - 13, socket.y + half - 13, 26, 26);
        context.strokeRect(socket.x + half - 13, socket.y + half - 13, 26, 26);
        const label = `#${socket.aruco_id} · ${Math.round(socket.size)}px`;
        context.font = "900 18px ui-monospace,monospace";
        const labelWidth = context.measureText(label).width + 24;
        const labelY = socket.y - half - 23;
        context.fillStyle = "#041019e8";
        context.strokeStyle = "#36dfff";
        context.lineWidth = 2;
        context.fillRect(socket.x - labelWidth / 2, labelY - 16, labelWidth, 32);
        context.strokeRect(socket.x - labelWidth / 2, labelY - 16, labelWidth, 32);
        context.fillStyle = "#fff";
        context.fillText(label, socket.x, labelY + 1);
      }
      context.restore();
    }

    function cachedEnemySprite(enemyType, frame, facingX, facingY) {
      const directions = 16;
      const rotation = Math.atan2(facingY, facingX) - Math.PI / 2;
      const direction = ((Math.round(rotation / (Math.PI * 2) * directions) % directions) + directions) % directions;
      const key = `${enemyType}:${frame}:${direction}`;
      if (enemySpriteCache.has(key)) return enemySpriteCache.get(key);
      const source = gameImages.get(`enemy:${enemyType}:${frame}`);
      if (!source) return null;
      const drawSize = (enemyType === "brute" ? 56 : 44) / 3;
      const canvasSize = 28;
      const sprite = document.createElement("canvas");
      sprite.width = canvasSize;
      sprite.height = canvasSize;
      const spriteContext = sprite.getContext("2d");
      spriteContext.imageSmoothingEnabled = false;
      spriteContext.translate(canvasSize / 2, canvasSize / 2);
      spriteContext.rotate(direction / directions * Math.PI * 2);
      spriteContext.drawImage(source, -drawSize / 2, -drawSize / 2, drawSize, drawSize);
      enemySpriteCache.set(key, sprite);
      return sprite;
    }

    function drawEnemy(context, enemy, visualTime, extrapolationAge) {
      const frame = 1 + (Math.floor(visualTime * 8 + Number(enemy.id || 0)) % 4);
      const size = (enemy.enemy_type === "brute" ? 56 : 44) / 3;
      const facingX = Number(enemy.facing_x ?? 0);
      const facingY = Number(enemy.facing_y ?? 1);
      const rawX = Number(enemy.x) + Number(enemy.vx || 0) * extrapolationAge;
      const rawY = Number(enemy.y) + Number(enemy.vy || 0) * extrapolationAge;
      const electrified = Number(enemy.electrocuted_until || 0) > visualTime;
      const intensity = Math.max(0, Math.min(1, Number(enemy.electrocution_intensity || 0)));
      const shake = electrified ? 2.5 + intensity * 3.5 : 0;
      const x = rawX + (electrified ? Math.sin(visualTime * 61 + Number(enemy.id)) * shake : 0);
      const y = rawY + (electrified ? Math.cos(visualTime * 47 + Number(enemy.id) * 1.7) * shake : 0);
      const sprite = cachedEnemySprite(enemy.enemy_type, frame, facingX, facingY);
      const history = enemyTrailHistory.get(enemy.id) || [];
      history.push({ x: rawX, y: rawY, at: visualTime });
      while (history.length > 7 || (history[0] && visualTime - history[0].at > 0.42)) history.shift();
      enemyTrailHistory.set(enemy.id, history);
      if (sprite) {
        if (electrified) {
          context.save();
          context.globalCompositeOperation = "lighter";
          for (let index = 0; index < history.length - 1; index += 1) {
            const trail = history[index];
            const alpha = (index + 1) / history.length * 0.16 * intensity;
            context.globalAlpha = alpha;
            context.drawImage(sprite, trail.x - sprite.width / 2, trail.y - sprite.height / 2);
          }
          context.globalAlpha = 0.65 + intensity * 0.35;
          context.shadowColor = "#a96cff";
          context.shadowBlur = 9 + intensity * 15;
          context.drawImage(sprite, x - sprite.width / 2, y - sprite.height / 2);
          context.restore();
        } else {
          context.drawImage(sprite, x - sprite.width / 2, y - sprite.height / 2);
        }
        if (Number(enemy.burn_until || 0) > visualTime) {
          const flicker = 0.7 + 0.3 * Math.sin(visualTime * 18 + Number(enemy.id));
          const flame = gameImages.get("effect:flame-burn");
          drawCombatEffect(context, flame, x, y - size * 0.45, size * 1.7, flicker);
        }
        return;
      }
      context.fillStyle = "#84c74a";
      context.beginPath();
      context.arc(x, y, size / 3, 0, Math.PI * 2);
      context.fill();
    }

    function towerPlacementId(tower) {
      return String(tower.placement_id || tower.socket_id);
    }

    function towerTargeting(tower) {
      const preview = towerAimPreview.get(towerPlacementId(tower));
      if (!preview) return tower.targeting || {};
      const angle = Number(preview.angle) * Math.PI / 180;
      const spread = Math.max(0, Math.min(1, Number(preview.spread)));
      if (tower.tower_type === "mortar") {
        const distance = 110 + (500 - 110) * spread;
        return { angle, angle_degrees: preview.angle, spread, range: distance, target_x: tower.x + Math.cos(angle) * distance, target_y: tower.y + Math.sin(angle) * distance, blast_radius: 72 + (155 - 72) * spread };
      }
      if (tower.tower_type === "tesla_coil") {
        return {
          angle,
          angle_degrees: preview.angle,
          spread,
          range: 120 + (265 - 120) * spread,
          min_range: 120,
          max_range: 265,
          damage_multiplier: 1.75 - 0.75 * spread,
          visual_intensity: 1 - 0.42 * spread,
          half_angle: 180,
        };
      }
      const config = tower.tower_type === "flamethrower"
        ? { near: 130, far: 235, narrow: 18, wide: 65 }
        : { near: 190, far: 360, narrow: 12, wide: 55 };
      return { angle, angle_degrees: preview.angle, spread, range: config.far + (config.near - config.far) * spread, half_angle: config.narrow + (config.wide - config.narrow) * spread };
    }

    function drawTargetingOverlay(context, tower) {
      if (tower.destroyed) return;
      const targeting = towerTargeting(tower);
      const selected = towerPlacementId(tower) === selectedTowerId;
      const color = tower.owner === "green" ? "53,208,127" : "192,132,252";
      context.save();
      context.strokeStyle = `rgba(${color},${selected ? 0.9 : 0.28})`;
      context.fillStyle = `rgba(${color},${selected ? 0.09 : 0.035})`;
      context.lineWidth = selected ? 2.5 : 1.25;
      context.setLineDash(selected ? [7, 5] : [4, 7]);
      if (tower.tower_type === "mortar") {
        context.beginPath();
        context.moveTo(tower.x, tower.y);
        context.lineTo(targeting.target_x, targeting.target_y);
        context.stroke();
        context.beginPath();
        context.arc(targeting.target_x, targeting.target_y, targeting.blast_radius, 0, Math.PI * 2);
        context.fill();
        context.stroke();
      } else if (tower.tower_type === "tesla_coil") {
        context.beginPath();
        context.arc(tower.x, tower.y, Number(targeting.range || 265), 0, Math.PI * 2);
        context.fill();
        context.stroke();
      } else {
        const half = Number(targeting.half_angle || 0) * Math.PI / 180;
        const angle = Number(targeting.angle || 0);
        const reach = Number(targeting.range || 0);
        context.beginPath();
        context.moveTo(tower.x, tower.y);
        context.lineTo(tower.x + Math.cos(angle - half) * reach, tower.y + Math.sin(angle - half) * reach);
        context.arc(tower.x, tower.y, reach, angle - half, angle + half);
        context.closePath();
        context.fill();
        context.stroke();
        context.beginPath();
        context.moveTo(tower.x, tower.y);
        context.lineTo(tower.x + Math.cos(angle) * reach, tower.y + Math.sin(angle) * reach);
        context.stroke();
      }
      context.restore();
    }

    function drawCombatEffect(context, image, x, y, size, alpha = 1, rotation = 0, stretch = 1) {
      if (!image) return;
      context.save();
      context.globalAlpha = Math.max(0, Math.min(1, alpha));
      context.translate(x, y);
      context.rotate(rotation);
      context.drawImage(image, -size * stretch / 2, -size / 2, size * stretch, size);
      context.restore();
    }

    function tintedEffect(image, color) {
      if (!image) return null;
      const key = `${image.src}:${color}`;
      if (tintedEffectCache.has(key)) return tintedEffectCache.get(key);
      const canvas = document.createElement("canvas");
      canvas.width = image.naturalWidth || image.width;
      canvas.height = image.naturalHeight || image.height;
      const context = canvas.getContext("2d");
      context.drawImage(image, 0, 0);
      context.globalCompositeOperation = "source-in";
      context.fillStyle = color;
      context.fillRect(0, 0, canvas.width, canvas.height);
      tintedEffectCache.set(key, canvas);
      return canvas;
    }

    function drawCoreSequence(context, gameState, visualTime, foreground = false) {
      const sequence = gameState.core_sequence || {};
      const stage = String(sequence.stage || "locked");
      const center = { x: Number(sequence.x ?? 880), y: Number(sequence.y ?? 480) };
      const pulse = 0.82 + Math.sin(visualTime * 6) * 0.1;
      if (!foreground && stage === "ring_ready") {
        drawCombatEffect(context, gameImages.get("effect:core-ring-aura"), center.x, center.y, 210, pulse);
      }
      if (!foreground && stage === "first_tag") {
        const greenAura = tintedEffect(gameImages.get("effect:core-ring-aura"), "#49ff88");
        drawCombatEffect(context, greenAura, center.x, center.y, 220, pulse);
        context.save();
        context.strokeStyle = `rgba(73,255,136,${pulse})`;
        context.shadowColor = "#49ff88";
        context.shadowBlur = 22;
        context.lineWidth = 7;
        context.beginPath();
        context.arc(center.x, center.y, 74, 0, Math.PI * 2);
        context.stroke();
        context.restore();
      }
      if (foreground && ["detonating", "complete"].includes(stage)) {
        const progress = Math.max(0, Math.min(1, Number(sequence.detonation_progress || 0)));
        if (progress < 0.32) {
          const burstAlpha = Math.max(0, 1 - progress / 0.34);
          drawCombatEffect(
            context,
            gameImages.get("effect:core-detonation-burst"),
            center.x,
            center.y,
            190 + progress * 260,
            burstAlpha,
          );
        }
        if (progress > 0 && progress < 1) {
          const radius = Number(sequence.detonation_radius || 0);
          drawCombatEffect(
            context,
            gameImages.get("effect:core-purge-wave"),
            center.x,
            center.y,
            Math.max(80, radius * 2.12),
            Math.min(1, 0.45 + (1 - progress) * 0.5),
          );
        }
      }
    }

    function drawSheetFrame(context, image, frame, frames, x, y, width, height, alpha = 1) {
      if (!image) return;
      const sourceWidth = (image.naturalWidth || image.width) / frames;
      const sourceHeight = image.naturalHeight || image.height;
      const index = ((Math.floor(frame) % frames) + frames) % frames;
      context.save();
      context.globalAlpha = Math.max(0, Math.min(1, alpha));
      context.drawImage(
        image,
        index * sourceWidth,
        0,
        sourceWidth,
        sourceHeight,
        x - width / 2,
        y - height,
        width,
        height,
      );
      context.restore();
    }

    function drawAtlasFrame(context, image, frame, columns, rows, x, y, width, height, alpha = 1, rotation = 0) {
      if (!image) return;
      const sourceWidth = (image.naturalWidth || image.width) / columns;
      const sourceHeight = (image.naturalHeight || image.height) / rows;
      const count = columns * rows;
      const index = ((Math.floor(frame) % count) + count) % count;
      const column = index % columns;
      const row = Math.floor(index / columns);
      context.save();
      context.globalAlpha = Math.max(0, Math.min(1, alpha));
      context.translate(x, y);
      context.rotate(rotation);
      context.drawImage(
        image,
        column * sourceWidth,
        row * sourceHeight,
        sourceWidth,
        sourceHeight,
        -width / 2,
        -height / 2,
        width,
        height,
      );
      context.restore();
    }

    function angleDelta(angle, reference) {
      return (angle - reference + Math.PI * 3) % (Math.PI * 2) - Math.PI;
    }

    function flamethrowerVisualAngleAt(tower, visualTime) {
      const targeting = towerTargeting(tower);
      const half = Number(targeting.half_angle || 0) * Math.PI / 180;
      const phaseOffset = (Number(tower.aruco_id || 0) % 7) / 7;
      const phase = ((visualTime / 1.6 + phaseOffset) % 1 + 1) % 1;
      const oscillation = 4 * Math.abs(phase - 0.5) - 1;
      return Number(targeting.angle || 0) + half * oscillation;
    }

    function drawFlamethrowerPilotFlame(context, tower, visualTime, turretAngle) {
      const delayedAngle = flamethrowerVisualAngleAt(
        tower,
        visualTime - FLAMETHROWER_PILOT_LAG_S,
      );
      const nozzle = flamethrowerNozzlePoint(tower.x, tower.y, turretAngle);
      const nozzleX = nozzle.x;
      const nozzleY = nozzle.y;
      const directionX = Math.cos(delayedAngle);
      const directionY = Math.sin(delayedAngle);
      const normalX = -directionY;
      const normalY = directionX;
      const seed = Number(tower.aruco_id || 0) * 0.37;
      const flicker = Math.sin(visualTime * 31 + seed) * 0.85
        + Math.sin(visualTime * 47 + seed * 1.9) * 0.45;
      const length = 9.5 + flicker;
      const width = 2.8 + Math.sin(visualTime * 37 + seed) * 0.35;
      const tipX = nozzleX + directionX * length;
      const tipY = nozzleY + directionY * length;

      context.save();
      context.globalCompositeOperation = "lighter";
      context.shadowColor = "#159dff";
      context.shadowBlur = 8;
      context.fillStyle = "#147dff";
      context.beginPath();
      context.moveTo(nozzleX + normalX * width, nozzleY + normalY * width);
      context.quadraticCurveTo(
        nozzleX + directionX * length * 0.46 + normalX * width * 0.72,
        nozzleY + directionY * length * 0.46 + normalY * width * 0.72,
        tipX,
        tipY,
      );
      context.quadraticCurveTo(
        nozzleX + directionX * length * 0.38 - normalX * width * 0.72,
        nozzleY + directionY * length * 0.38 - normalY * width * 0.72,
        nozzleX - normalX * width,
        nozzleY - normalY * width,
      );
      context.closePath();
      context.fill();

      context.shadowBlur = 4;
      context.fillStyle = "#c9f7ff";
      context.beginPath();
      context.moveTo(nozzleX + normalX * width * 0.38, nozzleY + normalY * width * 0.38);
      context.quadraticCurveTo(
        nozzleX + directionX * length * 0.34,
        nozzleY + directionY * length * 0.34,
        nozzleX + directionX * length * 0.68,
        nozzleY + directionY * length * 0.68,
      );
      context.quadraticCurveTo(
        nozzleX + directionX * length * 0.28,
        nozzleY + directionY * length * 0.28,
        nozzleX - normalX * width * 0.38,
        nozzleY - normalY * width * 0.38,
      );
      context.closePath();
      context.fill();
      context.restore();
    }

    function drawCurvedFlame(context, tower, visualTime, alpha) {
      const image = gameImages.get("effect:flame-gasoline");
      if (!image) return;
      const currentAngle = flamethrowerVisualAngleAt(tower, visualTime);
      const reach = Number(towerTargeting(tower).range || 180);
      const segmentLength = Math.max(1, reach - FLAMETHROWER_MUZZLE_OFFSET) / FLAMETHROWER_PATH_SEGMENTS;
      const pulse = 0.94 + Math.sin(visualTime * 34) * 0.06;
      const points = [flamethrowerNozzlePoint(
        tower.x, tower.y, currentAngle,
      )];
      for (let index = 0; index < FLAMETHROWER_PATH_SEGMENTS; index += 1) {
        const progress = (index + 1) / FLAMETHROWER_PATH_SEGMENTS;
        const delayedAngle = flamethrowerVisualAngleAt(
          tower,
          visualTime - progress * FLAMETHROWER_TRAIL_LAG_S,
        );
        const flutter = Math.sin(visualTime * 18 - index * 0.72) * 0.065 * progress;
        const previous = points[points.length - 1];
        points.push({
          x: previous.x + Math.cos(delayedAngle + flutter) * segmentLength,
          y: previous.y + Math.sin(delayedAngle + flutter) * segmentLength,
        });
      }
      context.save();
      context.globalAlpha = Math.max(0, Math.min(1, alpha));
      context.shadowColor = "#ff6a16";
      context.shadowBlur = 7;
      for (let index = FLAMETHROWER_PATH_SEGMENTS - 1; index >= 0; index -= 1) {
        const start = points[index];
        const end = points[index + 1];
        const angle = Math.atan2(end.y - start.y, end.x - start.x);
        const sourceX = Math.floor(image.width * index / FLAMETHROWER_PATH_SEGMENTS);
        const sourceRight = Math.ceil(image.width * (index + 1) / FLAMETHROWER_PATH_SEGMENTS);
        const destinationLength = Math.hypot(end.x - start.x, end.y - start.y) + 4;
        context.save();
        context.translate((start.x + end.x) / 2, (start.y + end.y) / 2);
        context.rotate(angle);
        context.drawImage(
          image,
          sourceX,
          0,
          Math.max(1, sourceRight - sourceX),
          image.height,
          -destinationLength / 2,
          -15 * pulse,
          destinationLength,
          30 * pulse,
        );
        context.restore();
      }
      context.restore();
    }

    function livePodVisualSize() {
      return LIVE_POD_SIZE;
    }

    function desiredTowerAngle(tower, visualTime) {
      if (tower.tower_type === "flamethrower") return flamethrowerVisualAngleAt(tower, visualTime);
      if (tower.tower_type === "tesla_coil") return 0;
      const firedAt = Number(tower.last_fire_at);
      if (tower.tower_type === "machine_gun" && Number.isFinite(firedAt) && visualTime - firedAt <= 0.32) {
        return Number(tower.facing_angle ?? tower.targeting?.angle ?? 0);
      }
      return Number(tower.targeting?.angle ?? tower.facing_angle ?? 0);
    }

    function smoothTowerAngle(tower, visualTime) {
      const id = towerPlacementId(tower);
      const desired = desiredTowerAngle(tower, visualTime);
      const previous = towerRenderAngles.has(id) ? towerRenderAngles.get(id) : desired;
      const response = tower.tower_type === "flamethrower" ? 1 : 0.24;
      const next = previous + angleDelta(desired, previous) * response;
      towerRenderAngles.set(id, next);
      return next;
    }

    function seededValue(seed) {
      const value = Math.sin(seed * 12.9898 + 78.233) * 43758.5453;
      return value - Math.floor(value);
    }

    function drawTeslaIdleCharge(context, tower, visualTime) {
      const firedAt = Number(tower.last_fire_at);
      const fireAge = visualTime - firedAt;
      if (
        Number.isFinite(firedAt)
        && fireAge >= 0
        && fireAge < TESLA_DISCHARGE_FLASH_S
      ) return;
      const snapshotTime = Number(state?.sim_time);
      const elapsed = Number.isFinite(snapshotTime)
        ? Math.max(0, visualTime - snapshotTime)
        : 0;
      const charge = advancedWeaponCharge(
        tower.weapon_charge,
        tower.charge_duration_s,
        elapsed,
      );
      if (charge <= 0.01) return;

      const towerSeed = Number(tower.aruco_id || 0) * 17.17;
      const frame = Math.floor(visualTime * (14 + charge * 10));
      const fluctuation = 0.76
        + 0.16 * Math.sin(visualTime * 19 + towerSeed)
        + 0.08 * Math.sin(visualTime * 37 + towerSeed * 1.7);
      const energy = Math.max(0.04, Math.min(1, charge * fluctuation));
      const x = Number(tower.x);
      const y = Number(tower.y);
      const outerRadius = 27;
      const terminalRadius = 8;

      context.save();
      context.globalCompositeOperation = "lighter";
      const halo = context.createRadialGradient(x, y, 2, x, y, 17 + charge * 8);
      halo.addColorStop(0, `rgba(244,249,255,${0.32 * energy})`);
      halo.addColorStop(0.28, `rgba(76,219,255,${0.25 * energy})`);
      halo.addColorStop(0.66, `rgba(142,83,255,${0.12 * energy})`);
      halo.addColorStop(1, "rgba(70,110,255,0)");
      context.fillStyle = halo;
      context.beginPath();
      context.arc(x, y, 17 + charge * 8, 0, Math.PI * 2);
      context.fill();

      const arcCount = 1 + Math.floor(charge * 4);
      context.lineCap = "round";
      context.lineJoin = "round";
      for (let arc = 0; arc < arcCount; arc += 1) {
        const seed = towerSeed + frame * 13 + arc * 31;
        const angle = Math.PI * 2 * (
          arc / arcCount + seededValue(seed) * 0.16
        );
        const points = [];
        for (let step = 0; step <= 4; step += 1) {
          const progress = step / 4;
          const radius = outerRadius * (1 - progress)
            + terminalRadius * progress;
          const bend = (
            seededValue(seed + step * 7) - 0.5
          ) * 0.62 * Math.sin(progress * Math.PI);
          points.push({
            x: x + Math.cos(angle + bend) * radius,
            y: y + Math.sin(angle + bend) * radius,
          });
        }
        for (const [color, width, alpha] of [
          ["#7447ff", 3.8, 0.34],
          ["#43dcff", 2.1, 0.72],
          ["#f4fbff", 0.8, 0.95],
        ]) {
          context.strokeStyle = color;
          context.lineWidth = width;
          context.globalAlpha = alpha * energy;
          context.shadowColor = color;
          context.shadowBlur = 7 * energy;
          context.beginPath();
          context.moveTo(points[0].x, points[0].y);
          for (let index = 1; index < points.length; index += 1) {
            context.lineTo(points[index].x, points[index].y);
          }
          context.stroke();
        }
      }

      context.globalAlpha = 0.5 + energy * 0.5;
      context.fillStyle = "#f4fbff";
      context.shadowColor = "#43dcff";
      context.shadowBlur = 8 + energy * 10;
      context.beginPath();
      context.arc(x, y, 1.8 + energy * 2.2, 0, Math.PI * 2);
      context.fill();
      context.restore();
    }

    function drawLightning(context, ax, ay, bx, by, intensity, seed, visualTime) {
      const dx = bx - ax;
      const dy = by - ay;
      const distance = Math.max(1, Math.hypot(dx, dy));
      const normalX = -dy / distance;
      const normalY = dx / distance;
      const steps = Math.max(5, Math.min(13, Math.round(distance / 22)));
      const frameSeed = Math.floor(visualTime * 28);
      const points = [];
      for (let index = 0; index <= steps; index += 1) {
        const progress = index / steps;
        const envelope = Math.sin(progress * Math.PI);
        const jitter = (seededValue(seed * 71 + frameSeed * 17 + index * 13) - 0.5) * 22 * envelope;
        points.push([ax + dx * progress + normalX * jitter, ay + dy * progress + normalY * jitter]);
      }
      context.save();
      context.lineCap = "round";
      context.lineJoin = "round";
      context.shadowColor = "#a96cff";
      context.shadowBlur = 14 * intensity;
      for (const [color, width, alpha] of [
        ["#7c35ff", 8 * intensity, 0.45],
        ["#ca8cff", 4 * intensity, 0.82],
        ["#f5f1ff", 1.7 * intensity, 1],
      ]) {
        context.strokeStyle = color;
        context.lineWidth = Math.max(0.8, width);
        context.globalAlpha = alpha * intensity;
        context.beginPath();
        context.moveTo(points[0][0], points[0][1]);
        for (let index = 1; index < points.length; index += 1) context.lineTo(points[index][0], points[index][1]);
        context.stroke();
      }
      context.restore();
    }

    function drawMortarEffects(context, gameState, visualTime) {
      const shell = gameImages.get("effect:mortar-shell");
      for (const projectile of gameState.projectiles || []) {
        const launchAt = Number(projectile.launch_at);
        const impactAt = Number(projectile.impact_at);
        const progress = Math.max(0, Math.min(1, (visualTime - launchAt) / Math.max(0.01, impactAt - launchAt)));
        const groundX = Number(projectile.origin_x) + (Number(projectile.target_x) - Number(projectile.origin_x)) * progress;
        const groundY = Number(projectile.origin_y) + (Number(projectile.target_y) - Number(projectile.origin_y)) * progress;
        const arc = Math.sin(progress * Math.PI);
        const size = 20 + arc * 64;
        context.save();
        context.globalAlpha = 0.22 + (1 - arc) * 0.18;
        context.fillStyle = "#000";
        context.beginPath();
        context.ellipse(groundX, groundY + 8, 10 + arc * 20, 4 + arc * 8, 0, 0, Math.PI * 2);
        context.fill();
        context.restore();
        const angle = Math.atan2(Number(projectile.target_y) - Number(projectile.origin_y), Number(projectile.target_x) - Number(projectile.origin_x));
        drawCombatEffect(context, shell, groundX, groundY - arc * 92, size, 1, angle + Math.PI / 2, 0.66);
      }
      for (const impact of gameState.mortar_impacts || []) {
        const age = visualTime - Number(impact.impact_at);
        if (age < 0 || age > 0.9) continue;
        const radius = Number(impact.blast_radius || 80);
        for (let index = 0; index < 8; index += 1) {
          const localAge = age - index * 0.055;
          if (localAge < 0 || localAge > 0.52) continue;
          const angle = seededValue(Number(impact.projectile_id) * 31 + index) * Math.PI * 2;
          const distance = Math.sqrt(seededValue(Number(impact.projectile_id) * 47 + index * 3)) * radius * 0.58;
          const x = Number(impact.x) + Math.cos(angle) * distance;
          const y = Number(impact.y) + Math.sin(angle) * distance;
          const alpha = Math.max(0, 1 - localAge / 0.52);
          drawCombatEffect(context, gameImages.get("effect:mortar-impact"), x, y, 55 + radius * 0.34, alpha, angle);
        }
      }
    }

    function drawTowerHealthEffects(context, tower, visualTime) {
      const maximum = Math.max(1, Number(tower.max_hp || 1));
      const ratio = Math.max(0, Math.min(1, Number(tower.hp || 0) / maximum));
      if (ratio >= 0.5 || tower.destroyed) return;
      const frame = Math.floor(visualTime * 8 + Number(tower.aruco_id || 0));
      const crackFrame = ratio < 0.1 ? 2 : ratio < 0.3 ? 1 : 0;
      const crackAlpha = 0.62 + Math.min(0.32, (0.5 - ratio) * 0.9);
      drawSheetFrame(
        context,
        gameImages.get("effect:tower-stress-cracks"),
        crackFrame,
        3,
        tower.x,
        tower.y + 44,
        92,
        92,
        crackAlpha,
      );
      if (ratio < 0.3) {
        const smokeSeverity = Math.min(1, (0.3 - ratio) / 0.3);
        for (let index = 0; index < 3; index += 1) {
          const phase = ((visualTime * (0.48 + index * 0.045) + index * 0.31 + Number(tower.aruco_id || 0) * 0.017) % 1 + 1) % 1;
          const envelope = Math.sin(phase * Math.PI);
          const x = Number(tower.x) + 7 + phase * (34 + smokeSeverity * 18) + Math.sin(visualTime * 3 + index) * 4;
          const y = Number(tower.y) + 7 - phase * (62 + smokeSeverity * 28);
          const width = (84 + smokeSeverity * 34) * (0.82 + index * 0.08);
          const height = (108 + smokeSeverity * 42) * (0.82 + index * 0.08);
          const alpha = envelope * (0.36 + smokeSeverity * 0.5) * (1 - index * 0.1);
          drawSheetFrame(context, gameImages.get("effect:tower-smoke"), frame + index, 4, x, y, width, height, alpha);
        }
      }
      if (ratio < 0.1) {
        const fireSeverity = Math.min(1, (0.1 - ratio) / 0.1);
        const fireWidth = 90 + fireSeverity * 35;
        const fireHeight = 108 + fireSeverity * 32;
        context.save();
        const glow = context.createRadialGradient(tower.x, tower.y, 4, tower.x, tower.y, 46 + fireSeverity * 20);
        glow.addColorStop(0, `rgba(255,245,180,${0.32 + fireSeverity * 0.3})`);
        glow.addColorStop(0.45, `rgba(255,102,20,${0.18 + fireSeverity * 0.2})`);
        glow.addColorStop(1, "rgba(255,60,8,0)");
        context.fillStyle = glow;
        context.beginPath();
        context.arc(tower.x, tower.y, 66 + fireSeverity * 18, 0, Math.PI * 2);
        context.fill();
        context.restore();
        drawSheetFrame(context, gameImages.get("effect:tower-fire"), frame, 4, tower.x - 7, tower.y + 40, fireWidth, fireHeight, 0.82 + fireSeverity * 0.18);
        drawSheetFrame(context, gameImages.get("effect:tower-fire"), frame + 2, 4, tower.x + 12, tower.y + 32, fireWidth * 0.72, fireHeight * 0.82, 0.58 + fireSeverity * 0.24);
        const emberCount = 4 + Math.round(fireSeverity * 8);
        for (let index = 0; index < emberCount; index += 1) {
          const seed = Number(tower.aruco_id || 0) * 37 + index * 19;
          const phase = ((visualTime * (1.4 + seededValue(seed) * 1.2) + seededValue(seed + 1)) % 1 + 1) % 1;
          const x = tower.x + (seededValue(seed + 2) - 0.35) * (46 + fireSeverity * 32) + phase * 16;
          const y = tower.y + 20 - phase * (58 + seededValue(seed + 3) * 42);
          const size = 2 + seededValue(seed + 4) * (3 + fireSeverity * 3);
          context.fillStyle = phase < 0.55 ? "#fff2a8" : phase < 0.82 ? "#ff9c24" : "#ff4c12";
          context.globalAlpha = Math.sin(phase * Math.PI) * (0.65 + fireSeverity * 0.35);
          context.fillRect(x, y, size, size);
        }
        context.globalAlpha = 1;
      }
    }

    function drawTowerDestruction(context, tower, visualTime) {
      const age = visualTime - Number(tower.destroyed_at);
      if (!Number.isFinite(age) || age < 0 || age > 4.6) return;
      if (age <= 0.4) {
        const blastProgress = Math.max(0, Math.min(1, age / 0.4));
        drawSheetFrame(
          context,
          gameImages.get("effect:tower-destruction-blast"),
          Math.min(3, Math.floor(blastProgress * 4)),
          4,
          tower.x,
          tower.y + 82,
          122 + blastProgress * 52,
          122 + blastProgress * 52,
          1 - Math.max(0, (blastProgress - 0.72) / 0.28),
        );
      }
      const flightDuration = 1.1;
      const settledUntil = flightDuration + 3.0;
      const debrisAlpha = age <= settledUntil
        ? 1
        : Math.max(0, 1 - (age - settledUntil) / 0.5);
      const flight = Math.max(0, Math.min(1, age / flightDuration));
      const debris = gameImages.get("effect:tower-debris");
      for (let index = 0; index < 8; index += 1) {
        const seed = Number(tower.aruco_id || 0) * 101 + index * 43;
        const angle = seededValue(seed) * Math.PI * 2;
        const distance = 52 + seededValue(seed + 1) * 50;
        const landingX = Number(tower.x) + Math.cos(angle) * distance;
        const landingY = Number(tower.y) + Math.sin(angle) * distance * 0.72;
        const groundX = Number(tower.x) + (landingX - Number(tower.x)) * flight;
        const groundY = Number(tower.y) + (landingY - Number(tower.y)) * flight;
        const arc = Math.sin(flight * Math.PI) * (54 + seededValue(seed + 2) * 42);
        const scale = 1 + arc / 105;
        const baseSize = 24 + seededValue(seed + 3) * 13;
        context.save();
        context.globalAlpha = debrisAlpha * (0.16 + flight * 0.18);
        context.fillStyle = "#020304";
        context.beginPath();
        context.ellipse(groundX, groundY + 5, baseSize * 0.46, baseSize * 0.19, angle, 0, Math.PI * 2);
        context.fill();
        context.restore();
        drawAtlasFrame(
          context,
          debris,
          index,
          4,
          2,
          groundX,
          groundY - arc,
          baseSize * scale,
          baseSize * scale,
          debrisAlpha,
          angle + flight * (3.2 + seededValue(seed + 4) * 4.5),
        );
      }
    }

    function drawTowerFireEffect(context, tower, visualTime) {
      const firedAt = Number(tower.last_fire_at);
      const target = tower.last_fire_target;
      if (!Number.isFinite(firedAt) || !target || tower.destroyed) return;
      const age = visualTime - firedAt;
      if (age < 0 || age > 0.58) return;
      const tx = Number(target.x);
      const ty = Number(target.y);
      if (tower.tower_type === "mortar") return;
      if (tower.tower_type === "tesla_coil") {
        let previous = { x: Number(tower.x), y: Number(tower.y) };
        const currentEnemies = new Map((state?.enemies || []).map((enemy) => [Number(enemy.id), enemy]));
        for (const link of tower.last_fire_chain || []) {
          const enemy = currentEnemies.get(Number(link.enemy_id));
          const next = enemy ? { x: Number(enemy.x), y: Number(enemy.y) } : { x: Number(link.x), y: Number(link.y) };
          const intensity = Math.max(0.14, Number(link.intensity || 0) * (1 - age / 0.58));
          drawLightning(context, previous.x, previous.y, next.x, next.y, intensity, Number(link.enemy_id), visualTime);
          drawCombatEffect(context, gameImages.get("effect:tesla-spark"), next.x, next.y, 32 + intensity * 23, intensity, visualTime * 2.2);
          previous = next;
        }
        return;
      }
      if (tower.tower_type === "flamethrower") {
        if (age > 0.34) return;
        drawCurvedFlame(context, tower, visualTime, 1 - age / 0.62);
        return;
      }
      if (tower.tower_type === "machine_gun" && age <= 0.24) {
        const liveTarget = (state?.enemies || []).find((enemy) => (
          Number(enemy.id) === Number(target.enemy_id)
        ));
        const targetX = liveTarget ? Number(liveTarget.x) : tx;
        const targetY = liveTarget ? Number(liveTarget.y) : ty;
        const fireAngle = Math.atan2(
          targetY - Number(tower.y), targetX - Number(tower.x)
        );
        const fireLines = machineGunFireLines(
          Number(tower.x),
          Number(tower.y),
          fireAngle,
          targetX,
          targetY,
        );
        for (let barrel = 0; barrel < fireLines.length; barrel += 1) {
          const line = fireLines[barrel];
          const dx = line.bx - line.ax;
          const dy = line.by - line.ay;
          const lineAngle = Math.atan2(dy, dx);
          drawCombatEffect(
            context,
            gameImages.get("effect:machine-gun-impact"),
            line.ax,
            line.ay,
            23,
            0.9,
            lineAngle,
          );
          for (let index = 0; index < 4; index += 1) {
            const progress = (
              (visualTime * 8.5 + index * 0.245 + barrel * 0.08) % 1 + 1
            ) % 1;
            const x = line.ax + dx * progress;
            const y = line.ay + dy * progress;
            drawCombatEffect(
              context,
              gameImages.get("effect:machine-gun-bullet"),
              x,
              y,
              10,
              0.95 - index * 0.09,
              lineAngle,
              2.8,
            );
          }
        }
        drawCombatEffect(
          context,
          gameImages.get("effect:machine-gun-impact"),
          targetX,
          targetY,
          42,
          1 - age / 0.24,
          fireAngle,
        );
      }
    }

    function arucoFieldKeepOuts(extraKeepOuts = []) {
      const levelProperties = propertyMap(level || {});
      const clearance = Number.isFinite(Number(
        levelProperties.force_field_marker_clearance_px
      ))
        ? Number(levelProperties.force_field_marker_clearance_px)
        : ARUCO_FIELD_CLEARANCE;
      const coreMarkerSize = Number.isFinite(Number(
        levelProperties.core_aruco_code_footprint_px
      ))
        ? Number(levelProperties.core_aruco_code_footprint_px)
        : CORE_MARKER_VISUAL_SIZE;
      const staticKeepOuts = socketRecords().map((socket) => ({
        markerId: socket.aruco_id,
        x: socket.marker_x,
        y: socket.marker_y,
        halfSize: socket.marker_size / 2,
      }));
      const core = coreMarkerRecord();
      staticKeepOuts.push({
        markerId: 38,
        x: core.x,
        y: core.y,
        halfSize: Math.max(coreMarkerSize, core.size) / 2,
      });
      return [...staticKeepOuts, ...extraKeepOuts]
        .map((keepOut) => normalizedKeepOut(keepOut, clearance))
        .filter(Boolean);
    }

    function drawForceFields(context, gameState, visualTime, extraKeepOuts = []) {
      context.save();
      context.lineCap = "round";
      const keepOuts = arucoFieldKeepOuts(extraKeepOuts);
      const forceFieldVisuals = Array.isArray(gameState.connections)
        ? gameState.connections.filter((field) => field.visible === true)
        : Array.isArray(gameState.force_field_visuals)
          ? gameState.force_field_visuals.filter((field) => field.visible !== false)
          : gameState.gates || [];
      for (const gate of forceFieldVisuals) {
        const durability = Math.max(0, Math.min(1, 1 - Number(gate.hits || 0) / Math.max(1, Number(gate.capacity || 1))));
        const pulse = 0.62 + 0.28 * Math.sin(visualTime * 5);
        const preview = gate.visual_state === "preview";
        const provisional = Boolean(gate.provisional);
        const broken = Boolean(gate.broken) || gate.visual_state === "broken";
        const invulnerable = Boolean(gate.invulnerable);
        context.strokeStyle = preview ? `rgba(54,223,255,${pulse * 0.68})` : broken ? `rgba(255,83,103,${pulse * 0.7})` : invulnerable ? `rgba(255,255,255,${pulse})` : durability > 0.5 ? `rgba(54,223,255,${pulse})` : durability > 0.2 ? `rgba(255,179,71,${pulse})` : `rgba(255,83,103,${pulse})`;
        context.shadowColor = preview ? "#36dfff" : invulnerable ? "#ffffff" : durability > 0.5 ? "#36dfff" : durability > 0.2 ? "#ffb347" : "#ff5367";
        context.shadowBlur = provisional ? 6 : preview ? 9 : 15;
        context.lineWidth = broken ? 3 : provisional ? 4 : preview ? 6 : 8;
        context.setLineDash(broken ? [12, 13] : provisional ? [18, 8] : []);
        const ax = Number(gate.ax);
        const ay = Number(gate.ay);
        const bx = Number(gate.bx);
        const by = Number(gate.by);
        const fieldLength = Math.hypot(bx - ax, by - ay);
        for (const segment of fieldSegmentsOutsideKeepOuts(ax, ay, bx, by, keepOuts)) {
          context.lineDashOffset = -fieldLength * segment.start;
          context.beginPath();
          context.moveTo(segment.ax, segment.ay);
          context.lineTo(segment.bx, segment.by);
          context.stroke();
        }
      }
      for (const impact of gameState.force_field_impacts || []) {
        const impactAge = visualTime - Number(impact.at);
        if (
          !Number.isFinite(impactAge)
          || impactAge < 0
          || impactAge > FORCE_FIELD_ZAP_DURATION_S
        ) continue;
        const impactX = Number(impact.contact_x);
        const impactY = Number(impact.contact_y);
        const impactSize = 72;
        const impactTouchesMarker = keepOuts.some((keepOut) => (
          circleOverlapsKeepOut(impactX, impactY, impactSize / 2, keepOut)
        ));
        if (impactTouchesMarker) continue;
        drawCombatEffect(
          context,
          gameImages.get("effect:force-field-impact"),
          impactX,
          impactY,
          impactSize,
          1 - impactAge / FORCE_FIELD_ZAP_DURATION_S,
        );
      }
      context.restore();
    }

    function drawForceFieldSkeletonZaps(
      context, gameState, visualTime, extrapolationAge
    ) {
      const skeleton = gameImages.get("effect:force-field-zap-skeleton");
      if (!skeleton) return;
      const enemiesById = new Map(
        (gameState.enemies || []).map((enemy) => [Number(enemy.id), enemy])
      );
      for (const impact of gameState.force_field_impacts || []) {
        const age = visualTime - Number(impact.at);
        if (
          !Number.isFinite(age)
          || age < 0
          || age > FORCE_FIELD_ZAP_DURATION_S
        ) continue;
        const enemy = enemiesById.get(Number(impact.enemy_id));
        const x = enemy
          ? Number(enemy.x) + Number(enemy.vx || 0) * extrapolationAge
          : Number(impact.enemy_x);
        const y = enemy
          ? Number(enemy.y) + Number(enemy.vy || 0) * extrapolationAge
          : Number(impact.enemy_y);
        const facingX = Number(enemy?.facing_x ?? impact.facing_x ?? 0);
        const facingY = Number(enemy?.facing_y ?? impact.facing_y ?? 1);
        const rotation = Math.atan2(facingY, facingX) - Math.PI / 2;
        const enemyType = String(enemy?.enemy_type || impact.enemy_type || "grunt");
        const baseSize = enemyType === "brute" ? 38 : 31;
        const pulse = 1 + Math.sin(visualTime * 48 + Number(impact.enemy_id)) * 0.06;
        const alpha = Math.max(0, 1 - age / FORCE_FIELD_ZAP_DURATION_S);
        context.save();
        context.globalCompositeOperation = "lighter";
        context.globalAlpha = alpha;
        context.shadowColor = "#36dfff";
        context.shadowBlur = 12 + alpha * 12;
        context.translate(x, y);
        context.rotate(rotation);
        context.drawImage(
          skeleton,
          -baseSize * pulse / 2,
          -baseSize * pulse / 2,
          baseSize * pulse,
          baseSize * pulse,
        );
        context.restore();
      }
    }

    function renderGame(now = performance.now()) {
      const context = gameCanvas.getContext("2d");
      context.clearRect(0, 0, WIDTH, HEIGHT);
      const fieldContext = fieldCanvas?.getContext("2d") || null;
      if (fieldContext) fieldContext.clearRect(0, 0, WIDTH, HEIGHT);
      if (!state || !level) return;
      context.imageSmoothingEnabled = false;
      const visualTime = visualSimulationTime(now, state);
      const extrapolationAge = state.phase === "running" && !state.paused
        ? Math.min(0.14, Math.max(0, now - stateReceivedAt) / 1000)
        : 0;
      for (const tower of state.towers || []) drawTargetingOverlay(context, tower);
      drawCoreSequence(context, state, visualTime, false);
      drawForceFields(context, state, visualTime);
      if (fieldContext) {
        drawForceFields(fieldContext, state, visualTime, detectedMarkerKeepOuts);
      }
      const socketsById = new Map(socketRecords().map((socket) => [String(socket.socket_id), socket]));
      for (const tower of state.towers || []) {
        if (tower.destroyed) {
          drawTowerDestruction(context, tower, visualTime);
          continue;
        }
        const socket = socketsById.get(String(tower.socket_id));
        const coverSize = livePodVisualSize(socket);
        const cover = gameImages.get("tower:socket-cover");
        if (cover) context.drawImage(cover, tower.x - coverSize / 2, tower.y - coverSize / 2, coverSize, coverSize);
        const base = gameImages.get(`tower:${tower.tower_type}:base`);
        const head = gameImages.get(`tower:${tower.tower_type}:head`);
        if (base) context.drawImage(base, tower.x - TOWER_VISUAL_SIZE / 2, tower.y - TOWER_VISUAL_SIZE / 2, TOWER_VISUAL_SIZE, TOWER_VISUAL_SIZE);
        if (head) {
          const angle = smoothTowerAngle(tower, visualTime);
          const pulse = tower.tower_type === "tesla_coil" ? 1 + Math.sin(visualTime * 8) * 0.035 : 1;
          context.save();
          context.translate(tower.x, tower.y);
          context.rotate(angle + Math.PI / 2);
          context.scale(pulse, pulse);
          context.drawImage(head, -TOWER_VISUAL_SIZE / 2, -TOWER_VISUAL_SIZE / 2, TOWER_VISUAL_SIZE, TOWER_VISUAL_SIZE);
          context.restore();
          if (tower.tower_type === "flamethrower") {
            drawFlamethrowerPilotFlame(context, tower, visualTime, angle);
          }
        }
        if (tower.tower_type === "tesla_coil") {
          drawTeslaIdleCharge(context, tower, visualTime);
        }
        context.fillStyle = tower.owner === "green" ? "#35d07f" : "#c084fc";
        context.beginPath();
        context.arc(tower.x, tower.y + 38, 11, 0, Math.PI * 2);
        context.fill();
        context.fillStyle = "#071018";
        context.font = "900 9px ui-monospace";
        context.textAlign = "center";
        context.fillText(String(tower.atom_tag_id), tower.x, tower.y + 41);
        const linkBonus = towerLinkMultiplierLabel(tower);
        context.fillStyle = "rgba(4,11,18,0.88)";
        context.fillRect(tower.x - 21, tower.y - 70, 42, 15);
        context.strokeStyle = linkBonus.linkMultiplier > 1
          ? "#36dfff"
          : linkBonus.linkMultiplier < 1
            ? "#ffb347"
            : "#dce9f5";
        context.lineWidth = 1;
        context.strokeRect(tower.x - 21, tower.y - 70, 42, 15);
        context.fillStyle = context.strokeStyle;
        context.font = "900 11px ui-monospace";
        context.fillText(linkBonus.label, tower.x, tower.y - 59);
        const health = towerHealthBarMetrics(tower, visualTime);
        context.fillStyle = "#240b10";
        context.fillRect(tower.x - 34, tower.y - 51, TOWER_HEALTH_BAR_WIDTH, 6);
        context.fillStyle = health.healthRatio > 0.5 ? "#35d07f" : health.healthRatio > 0.2 ? "#ffb347" : "#ff5367";
        context.fillRect(tower.x - 34, tower.y - 51, health.fillWidth, 6);
        if (health.damageAlpha > 0) {
          const notchX = tower.x - 34 + Math.max(
            0,
            Math.min(
              TOWER_HEALTH_BAR_WIDTH - health.damageNotchWidth,
              health.fillWidth,
            ),
          );
          context.fillStyle = `rgba(255,179,71,${0.72 + health.damageAlpha * 0.28})`;
          context.fillRect(notchX, tower.y - 51, health.damageNotchWidth, 6);
          context.save();
          context.strokeStyle = `rgba(255,83,103,${health.damageAlpha * 0.9})`;
          context.shadowColor = "#ff5367";
          context.shadowBlur = 8 + health.damageAlpha * 8;
          context.lineWidth = 2;
          context.beginPath();
          context.arc(tower.x, tower.y, TOWER_VISUAL_SIZE / 2 + 4, 0, Math.PI * 2);
          context.stroke();
          context.restore();
        }
        drawTowerHealthEffects(context, tower, visualTime);
      }
      const activeEnemyIds = new Set((state.enemies || []).map((enemy) => Number(enemy.id)));
      for (const enemyId of enemyTrailHistory.keys()) if (!activeEnemyIds.has(Number(enemyId))) enemyTrailHistory.delete(enemyId);
      for (const enemy of state.enemies || []) drawEnemy(context, enemy, visualTime, extrapolationAge);
      drawForceFieldSkeletonZaps(context, state, visualTime, extrapolationAge);
      drawMortarEffects(context, state, visualTime);
      for (const tower of state.towers || []) drawTowerFireEffect(context, tower, visualTime);
      drawCoreSequence(context, state, visualTime, true);
      renderCoreHealth(context, state);
      renderCoreMarkerOverlay(context, state);
      renderLayoutEditor(context);
    }

    function gameRenderLoop(now) {
      if (destroyed) return;
      const enemyCount = Number(state?.active_enemies || state?.enemies?.length || 0);
      const targetFps = enemyCount >= 800 ? 18 : enemyCount >= 400 ? 24 : 30;
      if (now - lastGameRenderAt >= 1000 / targetFps) {
        lastGameRenderAt = now;
        renderGame(now);
      }
      animationFrame = global.requestAnimationFrame(gameRenderLoop);
    }

    async function loadLevel() {
      const nextLevel = await fetchJson(`${root}/api/defence/level`);
      const nextTilesets = [];
      for (const reference of nextLevel.tilesets || []) {
        const tilesetUrl = urlFrom(reference.source, `${root}/assets/tiled/levels/z-pixel-first-map.tmj`);
        const source = await fetchJson(tilesetUrl);
        const tiles = new Map();
        for (const tile of source.tiles || []) {
          if (tile.image) tiles.set(tile.id, { ...tile, imageUrl: urlFrom(tile.image, tilesetUrl) });
        }
        nextTilesets.push({ firstgid: reference.firstgid, source, tiles });
      }
      level = nextLevel;
      tilesets = nextTilesets;
      markerImages.clear();
      const mapAssets = [...new Set(tilesets.flatMap((tileset) => (
        [...tileset.tiles.values()].map((tile) => tile.imageUrl)
      )))];
      const markerIds = [38, ...socketObjects().map((socket) => Number(propertyMap(socket).aruco_id))];
      await Promise.all([
        Promise.all(mapAssets.map(loadImage)),
        loadGameImages(),
        Promise.all(markerIds.map((markerId) => (
          loadImage(`${root}/api/defence/aruco/${markerId}.png`)
            .then((image) => markerImages.set(markerId, image))
        ))),
      ]);
      await renderMap();
      renderGame();
      const properties = propertyMap(level);
      return {
        level,
        levelId: properties.level_id || "unknown",
        revision: Number(properties.layout_revision || 1),
        layerCount: (level.layers || []).length,
      };
    }

    function applyState(nextState) {
      state = nextState;
      stateReceivedAt = performance.now();
    }

    function socketRecords() {
      return socketObjects().map((object) => {
        const properties = propertyMap(object);
        const center = tileObjectCenter(object);
        const markerCenter = arucoMarkerCenter(object);
        return {
          socket_id: String(properties.socket_id),
          owner: String(properties.owner),
          aruco_id: Number(properties.aruco_id),
          x: center.x,
          y: center.y,
          marker_x: markerCenter.x,
          marker_y: markerCenter.y,
          marker_size: socketMarkerVisualSize(),
          size: Number(object.width || object.height || 208),
        };
      });
    }

    function socketAtPoint(x, y) {
      return socketRecords()
        .filter((socket) => (
          Math.abs(socket.x - x) <= socket.size / 2
          && Math.abs(socket.y - y) <= socket.size / 2
        ))
        .sort((a, b) => Math.hypot(a.x - x, a.y - y) - Math.hypot(b.x - x, b.y - y))[0] || null;
    }

    function coreAtPoint(x, y) {
      const center = centralCoreCenter();
      return Math.hypot(center.x - x, center.y - y) <= 78
        ? { socket_id: "core_38", aruco_id: 38, x: center.x, y: center.y, size: 156 }
        : null;
    }

    function setSocketGeometry(socketId, geometry) {
      const object = socketObjects().find((candidate) => (
        String(propertyMap(candidate).socket_id) === String(socketId)
      ));
      if (!object) return null;
      if (Number.isFinite(Number(geometry.x))) object.x = Number(geometry.x);
      if (Number.isFinite(Number(geometry.y))) object.y = Number(geometry.y);
      if (Number.isFinite(Number(geometry.size))) {
        object.width = Number(geometry.size);
        object.height = Number(geometry.size);
      }
      updateGateGeometry();
      scheduleMapRender();
      renderGame();
      return socketRecords().find((socket) => socket.socket_id === String(socketId)) || null;
    }

    function setSocketLayout(records) {
      for (const record of records || []) setSocketGeometry(record.socket_id, record);
      updateGateGeometry();
      scheduleMapRender();
      renderGame();
    }

    function setDetectedMarkerKeepOuts(records) {
      detectedMarkerKeepOuts = (records || [])
        .map((record) => normalizedKeepOut(record))
        .filter(Boolean);
    }

    function setLayoutEditing(enabled) {
      layoutEditing = Boolean(enabled);
      if (!layoutEditing) selectedSocketId = null;
      renderGame();
    }

    function selectSocket(socketId) {
      selectedSocketId = socketId == null ? null : String(socketId);
      renderGame();
    }

    function selectTower(placementId) {
      selectedTowerId = placementId == null ? null : String(placementId);
      renderGame();
    }

    function previewTowerAim(placementId, angle, spread) {
      if (placementId == null) return;
      towerAimPreview.set(String(placementId), { angle: Number(angle), spread: Number(spread) });
      renderGame();
    }

    animationFrame = global.requestAnimationFrame(gameRenderLoop);

    return {
      applyState,
      destroy() {
        destroyed = true;
        if (animationFrame) global.cancelAnimationFrame(animationFrame);
      },
      get level() {
        return level;
      },
      get levelReady() {
        return Boolean(level);
      },
      loadLevel,
      renderGame,
      renderMap,
      selectSocket,
      selectTower,
      previewTowerAim,
      setLayoutEditing,
      setDetectedMarkerKeepOuts,
      setSocketGeometry,
      setSocketLayout,
      coreAtPoint,
      socketAtPoint,
      socketRecords,
    };
  }

  global.TowerDefenceView = {
    create: createTowerDefenceView,
    HEIGHT,
    WIDTH,
    geometry: {
      ARUCO_FIELD_CLEARANCE,
      advancedWeaponCharge,
      fieldSegmentsOutsideKeepOuts,
      fixedMarkerVisualSize,
      flamethrowerNozzlePoint,
      machineGunFireLines,
      machineGunMuzzlePoints,
      normalizedKeepOut,
      towerHealthBarMetrics,
      towerLinkMultiplierLabel,
    },
  };
})(typeof window === "undefined" ? globalThis : window);
