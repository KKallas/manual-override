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
    let level = null;
    let tilesets = [];
    let state = null;
    let stateReceivedAt = performance.now();
    let lastGameRenderAt = 0;
    let animationFrame = 0;
    let destroyed = false;

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

    function towerImagePath(type) {
      return `${root}/assets/game-art/z-pixel-v2/normalized/structures/${type.replace("_", "-")}-active-l1.png`;
    }

    function enemyImagePath(type, frame) {
      const group = type === "brute" ? "enemies-heavy-orcs-v2" : "enemies-light-orcs-v2";
      return `${root}/assets/game-art/sprites/${group}/${type}-walk-${String(frame).padStart(2, "0")}.png`;
    }

    async function loadGameImages() {
      const pending = [];
      for (const type of ["machine_gun", "flamethrower", "mortar"]) {
        pending.push(loadImage(towerImagePath(type)).then((image) => gameImages.set(`tower:${type}`, image)));
      }
      for (const type of ["grunt", "runner", "breaker", "brute"]) {
        for (let frame = 1; frame <= 4; frame += 1) {
          pending.push(loadImage(enemyImagePath(type, frame)).then((image) => {
            gameImages.set(`enemy:${type}:${frame}`, image);
          }));
        }
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
      const sockets = (level.layers || []).find((layer) => layer.name.includes("Placement Spots"));
      if (!sockets) return;
      context.font = "900 17px ui-monospace";
      context.textAlign = "center";
      context.textBaseline = "middle";
      for (const socket of sockets.objects || []) {
        const properties = propertyMap(socket);
        const center = tileObjectCenter(socket);
        context.strokeStyle = "#000";
        context.lineWidth = 5;
        context.strokeText(`#${properties.aruco_id}`, center.x, center.y);
        context.fillStyle = "#fff";
        context.fillText(`#${properties.aruco_id}`, center.x, center.y);
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
        context.restore();
        return;
      }
      context.fillStyle = "#84c74a";
      context.beginPath();
      context.arc(enemy.x, enemy.y, size / 3, 0, Math.PI * 2);
      context.fill();
    }

    function renderGame(now = performance.now()) {
      const context = gameCanvas.getContext("2d");
      context.clearRect(0, 0, WIDTH, HEIGHT);
      if (!state || !level) return;
      context.imageSmoothingEnabled = false;
      const visualTime = visualSimulationTime(now, state);
      context.save();
      context.lineCap = "round";
      for (const gate of state.gates || []) {
        const pulse = 0.62 + 0.28 * Math.sin(visualTime * 5);
        context.strokeStyle = `rgba(54,223,255,${pulse})`;
        context.shadowColor = "#36dfff";
        context.shadowBlur = 15;
        context.lineWidth = 8;
        context.beginPath();
        context.moveTo(gate.ax, gate.ay);
        context.lineTo(gate.bx, gate.by);
        context.stroke();
      }
      context.restore();
      for (const tower of state.towers || []) {
        const image = gameImages.get(`tower:${tower.tower_type}`);
        if (image) context.drawImage(image, tower.x - 56, tower.y - 56, 112, 112);
        context.fillStyle = tower.owner === "green" ? "#35d07f" : "#c084fc";
        context.beginPath();
        context.arc(tower.x, tower.y + 47, 15, 0, Math.PI * 2);
        context.fill();
        context.fillStyle = "#071018";
        context.font = "900 11px ui-monospace";
        context.textAlign = "center";
        context.fillText(String(tower.atom_tag_id), tower.x, tower.y + 51);
      }
      for (const enemy of state.enemies || []) drawEnemy(context, enemy, visualTime);
      renderCoreHealth(context, state);
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
      const mapAssets = [...new Set(tilesets.flatMap((tileset) => (
        [...tileset.tiles.values()].map((tile) => tile.imageUrl)
      )))];
      await Promise.all([Promise.all(mapAssets.map(loadImage)), loadGameImages()]);
      await renderMap();
      renderGame();
      return {
        level,
        levelId: propertyMap(level).level_id || "unknown",
        layerCount: (level.layers || []).length,
      };
    }

    function applyState(nextState) {
      state = nextState;
      stateReceivedAt = performance.now();
      renderGame();
    }

    function socketRecords() {
      const layer = (level?.layers || []).find((item) => item.name.includes("Placement Spots"));
      return (layer?.objects || []).map((object) => {
        const properties = propertyMap(object);
        const center = tileObjectCenter(object);
        return {
          socket_id: String(properties.socket_id),
          owner: String(properties.owner),
          aruco_id: Number(properties.aruco_id),
          x: center.x,
          y: center.y,
        };
      });
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
      socketRecords,
    };
  }

  global.TowerDefenceView = { create: createTowerDefenceView, HEIGHT, WIDTH };
})(typeof window === "undefined" ? globalThis : window);
