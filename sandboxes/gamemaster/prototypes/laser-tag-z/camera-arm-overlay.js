(function (global) {
  "use strict";

  const SVG_NS = "http://www.w3.org/2000/svg";

  function solveLinearSystem(matrix, values) {
    const count = values.length;
    const rows = matrix.map((row, index) => row.map(Number).concat(Number(values[index])));
    for (let column = 0; column < count; column += 1) {
      let pivot = column;
      for (let row = column + 1; row < count; row += 1) {
        if (Math.abs(rows[row][column]) > Math.abs(rows[pivot][column])) pivot = row;
      }
      if (Math.abs(rows[pivot][column]) < 1e-12) throw new Error("Cal 2 points do not span a stable 2D field");
      [rows[column], rows[pivot]] = [rows[pivot], rows[column]];
      const divisor = rows[column][column];
      for (let index = column; index <= count; index += 1) rows[column][index] /= divisor;
      for (let row = 0; row < count; row += 1) {
        if (row === column) continue;
        const factor = rows[row][column];
        for (let index = column; index <= count; index += 1) {
          rows[row][index] -= factor * rows[column][index];
        }
      }
    }
    return rows.map((row) => row[count]);
  }

  function tpsKernel(radiusSquared) {
    return radiusSquared <= 1e-16 ? 0 : radiusSquared * Math.log(radiusSquared) / 2;
  }

  function fitTps(points, outputKey) {
    const count = points.length;
    const size = count + 3;
    const matrix = Array.from({ length: size }, () => Array(size).fill(0));
    const values = Array(size).fill(0);
    for (let row = 0; row < count; row += 1) {
      for (let column = 0; column < count; column += 1) {
        const du = points[row].u - points[column].u;
        const dv = points[row].v - points[column].v;
        matrix[row][column] = tpsKernel(du * du + dv * dv);
      }
      matrix[row][row] += 1e-7;
      matrix[row][count] = 1;
      matrix[row][count + 1] = points[row].u;
      matrix[row][count + 2] = points[row].v;
      matrix[count][row] = 1;
      matrix[count + 1][row] = points[row].u;
      matrix[count + 2][row] = points[row].v;
      values[row] = points[row][outputKey];
    }
    return solveLinearSystem(matrix, values);
  }

  function evaluateTps(points, coefficients, u, v) {
    const count = points.length;
    let value = coefficients[count] + coefficients[count + 1] * u + coefficients[count + 2] * v;
    for (let index = 0; index < count; index += 1) {
      const du = u - points[index].u;
      const dv = v - points[index].v;
      value += coefficients[index] * tpsKernel(du * du + dv * dv);
    }
    return value;
  }

  function calibrationSamples(arm) {
    return Object.values(arm?.points || []).filter((point) => (
      point?.camera && point?.pose && point.pose.set
    )).map((point) => ({
      cameraU: Number(point.camera.u),
      cameraV: Number(point.camera.v),
      x: Number(point.pose.x),
      y: Number(point.pose.y),
    })).filter((point) => Object.values(point).every(Number.isFinite));
  }

  function projectionModel(arm) {
    const samples = calibrationSamples(arm);
    if (samples.length < 6) return null;
    const reverse = samples.map((point) => ({
      u: point.x / 500,
      v: point.y / 500,
      cameraU: point.cameraU,
      cameraV: point.cameraV,
    }));
    return {
      points: reverse,
      uCoefficients: fitTps(reverse, "cameraU"),
      vCoefficients: fitTps(reverse, "cameraV"),
    };
  }

  function projectPose(model, pose) {
    return {
      x: evaluateTps(model.points, model.uCoefficients, pose.x / 500, pose.y / 500),
      y: evaluateTps(model.points, model.vCoefficients, pose.x / 500, pose.y / 500),
    };
  }

  function armPose(state) {
    if (!state || state.connected !== true || !Array.isArray(state.pose) || state.pose.length < 3) return null;
    const pose = { x: Number(state.pose[0]), y: Number(state.pose[1]), z: Number(state.pose[2]) };
    return Object.values(pose).every(Number.isFinite) ? pose : null;
  }

  function clipSegment(start, end, margin = -0.03) {
    const maximum = 1 - margin;
    const dx = end.x - start.x;
    const dy = end.y - start.y;
    let enter = 0;
    let leave = 1;
    for (const [direction, distance] of [
      [-dx, start.x - margin],
      [dx, maximum - start.x],
      [-dy, start.y - margin],
      [dy, maximum - start.y],
    ]) {
      if (!direction) {
        if (distance < 0) return null;
        continue;
      }
      const amount = distance / direction;
      if (direction < 0) enter = Math.max(enter, amount);
      else leave = Math.min(leave, amount);
      if (enter > leave) return null;
    }
    return {
      start: { x: start.x + dx * enter, y: start.y + dy * enter },
      end: { x: start.x + dx * leave, y: start.y + dy * leave },
    };
  }

  function createArmOverlay(options) {
    const layer = options.layer;
    const fetchJson = options.fetchJson;
    const calibrationUrl = options.calibrationUrl;
    const staleMs = Number(options.staleMs || 1500);
    if (!layer || typeof fetchJson !== "function" || !calibrationUrl) {
      throw new Error("Arm overlay requires layer, fetchJson, and calibrationUrl");
    }

    let enabled = false;
    let width = 1696;
    let height = 960;
    let arms = {};
    let receivedAt = 0;
    let calibrationPromise = null;
    let models = {};

    function clear() {
      layer.replaceChildren();
      layer.style.display = "none";
    }

    function renderArm(side, state) {
      const model = models[side];
      const pose = armPose(state);
      if (!model || !pose) return [];
      try {
        const base = projectPose(model, { x: 0, y: 0 });
        const tip = projectPose(model, pose);
        if (![base.x, base.y, tip.x, tip.y].every(Number.isFinite)) return [];
        const clipped = clipSegment(base, tip);
        if (!clipped) return [];
        const bounds = layer.getBoundingClientRect();
        const displayWidth = bounds.width || width;
        const displayHeight = bounds.height || height;
        const distance = Math.hypot(
          (clipped.end.x - clipped.start.x) * displayWidth,
          (clipped.end.y - clipped.start.y) * displayHeight,
        );
        const squareCount = Math.max(1, Math.min(120, Math.ceil(distance / 9)));
        const nodes = [];
        const size = Math.max(8, Math.min(14, width * 0.0062));
        for (let index = 0; index <= squareCount; index += 1) {
          const amount = index / squareCount;
          const square = document.createElementNS(SVG_NS, "rect");
          const x = (clipped.start.x + (clipped.end.x - clipped.start.x) * amount) * width;
          const y = (clipped.start.y + (clipped.end.y - clipped.start.y) * amount) * height;
          square.setAttribute("class", `arm-square ${side}`);
          square.setAttribute("x", String(x - size / 2));
          square.setAttribute("y", String(y - size / 2));
          square.setAttribute("width", String(size));
          square.setAttribute("height", String(size));
          nodes.push(square);
        }
        if (tip.x >= -0.08 && tip.x <= 1.08 && tip.y >= -0.08 && tip.y <= 1.08) {
          const label = document.createElementNS(SVG_NS, "text");
          label.setAttribute("class", `arm-overlay-label ${side}`);
          label.setAttribute("x", String(Math.max(0.07, Math.min(0.93, tip.x)) * width));
          label.setAttribute("y", String(Math.max(0.07, Math.min(0.96, tip.y)) * height));
          label.textContent = `${side.toUpperCase()} ARM`;
          nodes.push(label);
        }
        return nodes;
      } catch (_) {
        return [];
      }
    }

    function render() {
      if (!enabled || !receivedAt || performance.now() - receivedAt > staleMs) {
        clear();
        return;
      }
      const nodes = ["purple", "green"].flatMap((side) => renderArm(side, arms[side]));
      layer.replaceChildren(...nodes);
      layer.style.display = nodes.length ? "" : "none";
    }

    async function loadCalibration() {
      if (calibrationPromise) return calibrationPromise;
      calibrationPromise = fetchJson(calibrationUrl).then((data) => {
        const calibratedArms = data.calibration?.arms || {};
        models = {};
        for (const side of ["green", "purple"]) {
          const model = projectionModel(calibratedArms[side]);
          if (model) models[side] = model;
        }
        render();
        return { calibratedSides: Object.keys(models) };
      }).catch((error) => {
        calibrationPromise = null;
        models = {};
        clear();
        throw error;
      });
      return calibrationPromise;
    }

    return {
      clear,
      expire() {
        render();
      },
      loadCalibration,
      setEnabled(value) {
        enabled = Boolean(value);
        if (!enabled) clear();
        else render();
      },
      setFrame(nextWidth, nextHeight) {
        width = Number(nextWidth) > 0 ? Number(nextWidth) : 1696;
        height = Number(nextHeight) > 0 ? Number(nextHeight) : 960;
        layer.setAttribute("viewBox", `0 0 ${width} ${height}`);
        render();
      },
      update(nextArms) {
        arms = nextArms && typeof nextArms === "object" ? nextArms : {};
        receivedAt = performance.now();
        render();
      },
    };
  }

  global.LaserTagArmOverlay = { create: createArmOverlay };
})(typeof window === "undefined" ? globalThis : window);
