/**
 * Web Worker for cross-catalog perceptual hash comparison.
 * Finds all pairs (A, B) where hamming distance ≤ threshold.
 *
 * Input:  { hashPairsA: [[path, hi, lo], ...], hashPairsB: [[path, hi, lo], ...], threshold }
 * Output: { type: 'progress', percent } during computation
 *         { type: 'complete', matches: [[pathA, pathB, distance], ...] }
 */

function popcount32(n) {
    n = n >>> 0;
    n -= (n >>> 1) & 0x55555555;
    n = (n & 0x33333333) + ((n >>> 2) & 0x33333333);
    n = (n + (n >>> 4)) & 0x0f0f0f0f;
    return (n * 0x01010101) >>> 24;
}

self.onmessage = function(event) {
    const { hashPairsA, hashPairsB, threshold } = event.data;
    const matches = [];
    const total = hashPairsA.length;

    for (let i = 0; i < hashPairsA.length; i++) {
        const [pathA, hiA, loA] = hashPairsA[i];
        for (const [pathB, hiB, loB] of hashPairsB) {
            const d = popcount32(hiA ^ hiB) + popcount32(loA ^ loB);
            if (d <= threshold) {
                matches.push([pathA, pathB, d]);
            }
        }
        if (i % 500 === 0) {
            self.postMessage({ type: 'progress', percent: Math.round(i / total * 100) });
        }
    }

    matches.sort((a, b) => a[2] - b[2]);
    self.postMessage({ type: 'complete', matches });
};
