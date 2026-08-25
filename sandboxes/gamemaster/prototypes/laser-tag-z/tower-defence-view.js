(function (global) {
  "use strict";

  const WIDTH = 1696;
  const HEIGHT = 960;

  function propertyMap(object) {
    return Object.fromEntries((object.properties || []).map((item) => [item.name, item.value]));
  }

  function createTowerDefenceView(options) {
    const root = String(options.root || "").replace(/\/$/, "");
    const mapCanvas = options.mapCanvas;
    const gameCanvas = options.gameCanvas;
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
    let selectedTowerTag = null;
    const towerAimPreview = new Map();
    let mapRenderQueued = false;

    mapCanvas.width = WIDTH;
    mapCanvas.height = HEIGHT;
    gameCanvas.width = WIDTH;
    gameCanvas.height = HEIGHT;

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

    function towerImagePath(type, stateName = "active-l1") {
      return `${root}/assets/game-art/z-pixel-v2/normalized/structures/${type.replace("_", "-")}-${stateName}.png`;
    }

    function enemyImagePath(type, frame) {
      const group = type === "brute" ? "enemies-heavy-orcs-v2" : "enemies-light-orcs-v2";
      return `${root}/assets/game-art/sprites/${group}/${type}-walk-${String(frame).padStart(2, "0")}.png`;
    }

    function combatEffectPath(name) {
      return `${root}/assets/game-art/z-pixel-v2/normalized/effects/combat/${name}-v1.png`;
    }

    async function loadGameImages() {
      const pending = [];
      for (const type of ["machine_gun", "flamethrower", "mortar"]) {
        pending.push(loadImage(towerImagePath(type)).then((image) => gameImages.set(`tower:${type}`, image)));
        pending.push(loadImage(towerImagePath(type, "damaged")).then((image) => gameImages.set(`tower:${type}:damaged`, image)));
      }
      for (const type of ["grunt", "runner", "breaker", "brute"]) {
        for (let frame = 1; frame <= 4; frame += 1) {
          pending.push(loadImage(enemyImagePath(type, frame)).then((image) => {
            gameImages.set(`enemy:${type}:${frame}`, image);
          }));
        }
      }
      for (const effect of ["machine-gun-impact", "flame-burn", "mortar-impact", "force-field-impact"]) {
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

    function tileObjectCenter(object) {
      const resolved = tileForGid(object.gid);
      if (!resolved) return { x: Number(object.x || 0), y: Number(object.y || 0) };
      const width = Number(object.width || resolved.tile.imagewidth);
      const height = Number(object.height || resolved.tile.imageheight);
      const { dx, dy } = tileDrawOffset(resolved.objectAlignment, width, height);
      const localX = dx + width / 2;
      const localY = dy + height / 2;
      const rotation = Number(object.rotation || 0) * Math.PI / 180;
      const cos = Math.cos(rotation);
      const sin = Math.sin(rotation);
      return {
        x: Number(object.x || 0) + localX * cos - localY * sin,
        y: Number(object.y || 0) + localX * sin + localY * cos,
      };
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
        const center = tileObjectCenter(socket);
        const marker = markerImages.get(Number(properties.aruco_id));
        if (!marker) continue;
        const width = Number(socket.width || 208);
        const height = Number(socket.height || width);
        const markerSize = Math.max(42, Math.min(104, Math.round(Math.min(width, height) * 0.37)));
        context.imageSmoothingEnabled = false;
        context.drawImage(
          marker,
          Math.round(center.x - markerSize / 2),
          Math.round(center.y - markerSize / 2),
          markerSize,
          markerSize,
        );
      }
    }

    function visualSimulationTime(now, gameState) {
      const serverTime = Number(gameState.sim_time || 0);
      const advancing = gameState.phase === "running" && !gameState.paused;
      return serverTime + (advancing ? Math.max(0, now - stateReceivedAt) / 1000 : 0);
    }

    function centralCoreCenter() {
      const layer = (level?.layers || []).find((item) => item.name.includes("Central Square Core"));
      const object = (layer?.objects || []).find((item) => item.name === "central_core_square_base");
      return object ? tileObjectCenter(object) : { x: 880, y: 480 };
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

    function drawEnemy(context, enemy, visualTime) {
      const frame = 1 + (Math.floor(visualTime * 8 + Number(enemy.id || 0)) % 4);
      const image = gameImages.get(`enemy:${enemy.enemy_type}:${frame}`);
      const size = (enemy.enemy_type === "brute" ? 56 : 44) / 3;
      const facingX = Number(enemy.facing_x ?? 0);
      const facingY = Number(enemy.facing_y ?? 1);
      const rotation = Math.atan2(facingY, facingX) - Math.PI / 2;
      if (image) {
        context.save();
        context.translate(enemy.x, enemy.y);
        context.rotate(rotation);
        context.drawImage(image, -size / 2, -size / 2, size, size);
        if (Number(enemy.burn_until || 0) > visualTime) {
          const flicker = 0.7 + 0.3 * Math.sin(visualTime * 18 + Number(enemy.id));
          const flame = gameImages.get("effect:flame-burn");
          context.globalAlpha = flicker;
          if (flame) context.drawImage(flame, -size * 0.85, -size * 1.5, size * 1.7, size * 1.7);
        }
        context.restore();
        return;
      }
      context.fillStyle = "#84c74a";
      context.beginPath();
      context.arc(enemy.x, enemy.y, size / 3, 0, Math.PI * 2);
      context.fill();
    }

    function towerTargeting(tower) {
      const preview = towerAimPreview.get(Number(tower.atom_tag_id));
      if (!preview) return tower.targeting || {};
      const angle = Number(preview.angle) * Math.PI / 180;
      const spread = Math.max(0, Math.min(1, Number(preview.spread)));
      if (tower.tower_type === "mortar") {
        const distance = 110 + (500 - 110) * spread;
        return { angle, angle_degrees: preview.angle, spread, range: distance, target_x: tower.x + Math.cos(angle) * distance, target_y: tower.y + Math.sin(angle) * distance, blast_radius: 72 + (155 - 72) * spread };
      }
      const config = tower.tower_type === "flamethrower"
        ? { near: 130, far: 235, narrow: 18, wide: 65 }
        : { near: 190, far: 360, narrow: 12, wide: 55 };
      return { angle, angle_degrees: preview.angle, spread, range: config.far + (config.near - config.far) * spread, half_angle: config.narrow + (config.wide - config.narrow) * spread };
    }

    function drawTargetingOverlay(context, tower) {
      if (tower.destroyed) return;
      const targeting = towerTargeting(tower);
      const selected = Number(tower.atom_tag_id) === Number(selectedTowerTag);
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

    function drawTowerFireEffect(context, tower, visualTime) {
      const firedAt = Number(tower.last_fire_at);
      const target = tower.last_fire_target;
      if (!Number.isFinite(firedAt) || !target) return;
      const age = visualTime - firedAt;
      if (age < 0 || age > 0.45) return;
      const tx = Number(target.x);
      const ty = Number(target.y);
      if (tower.tower_type === "mortar") {
        drawCombatEffect(context, gameImages.get("effect:mortar-impact"), tx, ty, 118, 1 - age / 0.45);
        return;
      }
      const dx = tx - tower.x;
      const dy = ty - tower.y;
      if (tower.tower_type === "flamethrower") {
        if (age > 0.24) return;
        const distance = Math.max(1, Math.hypot(dx, dy));
        drawCombatEffect(
          context,
          gameImages.get("effect:flame-burn"),
          tower.x + dx / 2,
          tower.y + dy / 2,
          64,
          0.9 * (1 - age / 0.24),
          Math.atan2(dy, dx),
          Math.max(1, distance / 64),
        );
        return;
      }
      if (age <= 0.16) {
        drawCombatEffect(context, gameImages.get("effect:machine-gun-impact"), tx, ty, 55, 1 - age / 0.16);
      }
    }

    function renderGame(now = performance.now()) {
      const context = gameCanvas.getContext("2d");
      context.clearRect(0, 0, WIDTH, HEIGHT);
      if (!state || !level) return;
      context.imageSmoothingEnabled = false;
      const visualTime = visualSimulationTime(now, state);
      for (const tower of state.towers || []) drawTargetingOverlay(context, tower);
      context.save();
      context.lineCap = "round";
      for (const gate of state.gates || []) {
        const durability = Math.max(0, Math.min(1, 1 - Number(gate.hits || 0) / Math.max(1, Number(gate.capacity || 1))));
        const pulse = 0.62 + 0.28 * Math.sin(visualTime * 5);
        const broken = Boolean(gate.broken);
        context.strokeStyle = broken ? `rgba(255,83,103,${pulse * 0.7})` : durability > 0.5 ? `rgba(54,223,255,${pulse})` : durability > 0.2 ? `rgba(255,179,71,${pulse})` : `rgba(255,83,103,${pulse})`;
        context.shadowColor = durability > 0.5 ? "#36dfff" : durability > 0.2 ? "#ffb347" : "#ff5367";
        context.shadowBlur = 15;
        context.lineWidth = broken ? 3 : 8;
        context.setLineDash(broken ? [12, 13] : []);
        context.beginPath();
        context.moveTo(gate.ax, gate.ay);
        context.lineTo(gate.bx, gate.by);
        context.stroke();
        const impactAge = visualTime - Number(gate.last_hit_at);
        if (Number.isFinite(impactAge) && impactAge >= 0 && impactAge <= 0.42) {
          drawCombatEffect(
            context,
            gameImages.get("effect:force-field-impact"),
            (gate.ax + gate.bx) / 2,
            (gate.ay + gate.by) / 2,
            broken ? 105 : 72,
            1 - impactAge / 0.42,
          );
        }
      }
      context.restore();
      for (const tower of state.towers || []) {
        const image = gameImages.get(`tower:${tower.tower_type}${tower.destroyed?':damaged':''}`);
        if (image) context.drawImage(image, tower.x - 56, tower.y - 56, 112, 112);
        context.fillStyle = tower.owner === "green" ? "#35d07f" : "#c084fc";
        context.beginPath();
        context.arc(tower.x, tower.y + 47, 15, 0, Math.PI * 2);
        context.fill();
        context.fillStyle = "#071018";
        context.font = "900 11px ui-monospace";
        context.textAlign = "center";
        context.fillText(String(tower.atom_tag_id), tower.x, tower.y + 51);
        const healthRatio = Math.max(0, Math.min(1, Number(tower.hp || 0) / Math.max(1, Number(tower.max_hp || 1))));
        context.fillStyle = "#240b10";
        context.fillRect(tower.x - 43, tower.y - 65, 86, 7);
        context.fillStyle = healthRatio > 0.5 ? "#35d07f" : healthRatio > 0.2 ? "#ffb347" : "#ff5367";
        context.fillRect(tower.x - 43, tower.y - 65, 86 * healthRatio, 7);
      }
      for (const enemy of state.enemies || []) drawEnemy(context, enemy, visualTime);
      for (const tower of state.towers || []) drawTowerFireEffect(context, tower, visualTime);
      renderCoreHealth(context, state);
      renderLayoutEditor(context);
    }

    function gameRenderLoop(now) {
      if (destroyed) return;
      if (now - lastGameRenderAt >= 1000 / 30) {
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
      const markerIds = socketObjects().map((socket) => Number(propertyMap(socket).aruco_id));
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
      renderGame();
    }

    function socketRecords() {
      return socketObjects().map((object) => {
        const properties = propertyMap(object);
        const center = tileObjectCenter(object);
        return {
          socket_id: String(properties.socket_id),
          owner: String(properties.owner),
          aruco_id: Number(properties.aruco_id),
          x: center.x,
          y: center.y,
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

    function setLayoutEditing(enabled) {
      layoutEditing = Boolean(enabled);
      if (!layoutEditing) selectedSocketId = null;
      renderGame();
    }

    function selectSocket(socketId) {
      selectedSocketId = socketId == null ? null : String(socketId);
      renderGame();
    }

    function selectTower(atomTagId) {
      selectedTowerTag = atomTagId == null ? null : Number(atomTagId);
      renderGame();
    }

    function previewTowerAim(atomTagId, angle, spread) {
      if (atomTagId == null) return;
      towerAimPreview.set(Number(atomTagId), { angle: Number(angle), spread: Number(spread) });
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
      setSocketGeometry,
      setSocketLayout,
      socketAtPoint,
      socketRecords,
    };
  }

  global.TowerDefenceView = { create: createTowerDefenceView, HEIGHT, WIDTH };
})(typeof window === "undefined" ? globalThis : window);
