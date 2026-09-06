"""Browser-side JavaScript used by the post-deploy visual gate."""

PIXEL_DIFF = """
async ({ actualPng, expectedPng, threshold }) => {
  const load = (png) => new Promise((resolve, reject) => {
    const image = new Image();
    image.onload = () => resolve(image);
    image.onerror = () => reject(new Error("could not decode screenshot"));
    image.src = `data:image/png;base64,${png}`;
  });
  const [actual, expected] = await Promise.all([load(actualPng), load(expectedPng)]);
  const totalPixels = Math.max(actual.width * actual.height, expected.width * expected.height);
  if (actual.width !== expected.width || actual.height !== expected.height) {
    return { totalPixels, regions: [{x: 0, y: 0, width: Math.max(actual.width, expected.width),
      height: Math.max(actual.height, expected.height), pixels: totalPixels}] };
  }
  const canvas = document.createElement("canvas");
  canvas.width = actual.width; canvas.height = actual.height;
  const context = canvas.getContext("2d", {willReadFrequently: true});
  if (!context) throw new Error("could not create screenshot comparison canvas");
  context.drawImage(actual, 0, 0);
  const a = context.getImageData(0, 0, actual.width, actual.height).data;
  context.clearRect(0, 0, actual.width, actual.height);
  context.drawImage(expected, 0, 0);
  const e = context.getImageData(0, 0, expected.width, expected.height).data;
  const tile = 8; const regions = [];
  for (let y = 0; y < actual.height; y += tile) {
    for (let x = 0; x < actual.width; x += tile) {
      let pixels = 0;
      for (let py = y; py < Math.min(y + tile, actual.height); py += 1) {
        for (let px = x; px < Math.min(x + tile, actual.width); px += 1) {
          const i = (py * actual.width + px) * 4;
          const delta = Math.max(Math.abs(a[i] - e[i]), Math.abs(a[i+1] - e[i+1]),
            Math.abs(a[i+2] - e[i+2]), Math.abs(a[i+3] - e[i+3]));
          if (delta > threshold) pixels += 1;
        }
      }
      if (pixels) regions.push({x, y, width: Math.min(tile, actual.width - x),
        height: Math.min(tile, actual.height - y), pixels});
    }
  }
  return {totalPixels, regions};
}
"""

OVERLAY = """
async ({ png, regions }) => {
  const image = await new Promise((resolve, reject) => {
    const value = new Image(); value.onload = () => resolve(value);
    value.onerror = reject; value.src = `data:image/png;base64,${png}`;
  });
  const canvas = document.createElement("canvas");
  canvas.width = image.width; canvas.height = image.height;
  const context = canvas.getContext("2d");
  if (!context) throw new Error("could not create screenshot overlay canvas");
  context.drawImage(image, 0, 0);
  context.fillStyle = "rgba(255, 0, 0, 0.22)"; context.strokeStyle = "#ff0000";
  for (const region of regions) {
    context.fillRect(region.x, region.y, region.width, region.height);
    context.strokeRect(region.x + 0.5, region.y + 0.5, region.width - 1, region.height - 1);
  }
  return canvas.toDataURL("image/png").split(",", 2)[1];
}
"""
