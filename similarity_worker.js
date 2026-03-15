/**
 * Web Worker for calculating per-page image similarities.
 * Uses fast 32-bit integer hamming distance instead of BigInt.
 *
 * Protocol:
 *   1. { type: 'init', allHashPairs: [[fullPath, hiUint32, loUint32], ...] }
 *      Sent once after data loads. Worker stores hash pairs in memory.
 *
 *   2. { type: 'compute', pageRows: [{FullPath, PerceptualHash}], similarityThreshold }
 *      Sent on each page render. Worker returns results for just these rows.
 *      Response: [[fullPath, [{path, distance}, ...]], ...]
 */

let storedHashPairs = null;

function popcount32(n) {
    n = n >>> 0;
    n -= (n >>> 1) & 0x55555555;
    n = (n & 0x33333333) + ((n >>> 2) & 0x33333333);
    n = (n + (n >>> 4)) & 0x0f0f0f0f;
    return (n * 0x01010101) >>> 24;
}

function computeSimilarities(pageRows, allHashPairs, threshold) {
    const results = [];
    for (const row of pageRows) {
        if (!row.PerceptualHash || row.PerceptualHash.length < 16) {
            results.push([row.FullPath, []]);
            continue;
        }
        const hiA = parseInt(row.PerceptualHash.slice(0, 8), 16) >>> 0;
        const loA = parseInt(row.PerceptualHash.slice(8, 16), 16) >>> 0;

        const similar = [];
        for (const pair of allHashPairs) {
            if (pair[0] === row.FullPath) continue;
            const d = popcount32(hiA ^ pair[1]) + popcount32(loA ^ pair[2]);
            if (d <= threshold) {
                similar.push({ path: pair[0], distance: d });
            }
        }
        similar.sort((a, b) => a.distance - b.distance);
        results.push([row.FullPath, similar]);
    }
    return results;
}

self.onmessage = function(event) {
    const { type } = event.data;

    if (type === 'init') {
        storedHashPairs = event.data.allHashPairs;
        console.log(`Worker: Initialized with ${storedHashPairs.length} hash pairs.`);
        return;
    }

    if (type === 'compute') {
        const { pageRows, similarityThreshold } = event.data;
        if (!storedHashPairs) {
            self.postMessage([]);
            return;
        }
        const results = computeSimilarities(pageRows, storedHashPairs, similarityThreshold);
        self.postMessage(results);
    }
};
