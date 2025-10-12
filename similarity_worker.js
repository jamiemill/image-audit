/**
 * Web Worker for calculating image similarities in the background.
 */

// --- Calculation Functions (copied from main script) ---

function hammingDistance(hex1, hex2) {
    if (!hex1 || !hex2 || hex1.length !== hex2.length) return Infinity;
    const bigInt1 = BigInt(`0x${hex1}`);
    const bigInt2 = BigInt(`0x${hex2}`);
    let xorResult = bigInt1 ^ bigInt2;
    let distance = 0;
    while (xorResult > 0) {
        distance += Number(xorResult & 1n);
        xorResult >>= 1n;
    }
    return distance;
}

function calculateAllSimilarities(data, similarityThreshold) {
    const simMap = new Map();
    data.forEach(item => simMap.set(item.FullPath, []));

    const hashIndex = data.reduce((acc, item) => {
        if (item.PerceptualHash) {
            if (!acc[item.PerceptualHash]) acc[item.PerceptualHash] = [];
            acc[item.PerceptualHash].push(item);
        }
        return acc;
    }, {});

    const uniqueHashes = Object.keys(hashIndex);

    for (let i = 0; i < uniqueHashes.length; i++) {
        for (let j = i; j < uniqueHashes.length; j++) {
            const hashA = uniqueHashes[i];
            const hashB = uniqueHashes[j];
            const distance = hammingDistance(hashA, hashB);

            if (distance <= similarityThreshold) {
                const itemsA = hashIndex[hashA];
                const itemsB = hashIndex[hashB];
                itemsA.forEach(itemA => {
                    itemsB.forEach(itemB => {
                        if (itemA.FullPath !== itemB.FullPath) {
                            simMap.get(itemA.FullPath).push({ ...itemB, distance });
                            simMap.get(itemB.FullPath).push({ ...itemA, distance });
                        }
                    });
                });
            }
        }
    }

    for (const key of simMap.keys()) {
        const similarItems = simMap.get(key);
        const uniqueSimilarItems = Array.from(new Map(similarItems.map(item => [item.FullPath, item])).values());
        uniqueSimilarItems.sort((a, b) => a.distance - b.distance);
        simMap.set(key, uniqueSimilarItems);
    }
    return simMap;
}

// --- Worker Message Handling ---

self.onmessage = function(event) {
    console.log("Worker: Received data. Starting similarity calculation.");
    const { data, similarityThreshold } = event.data;
    const similarityMap = calculateAllSimilarities(data, similarityThreshold);
    console.log("Worker: Calculation complete. Sending results back.");
    // Convert Map to array of pairs for sending, as Map object itself might not be structured-clonable in all contexts.
    const a = Array.from(similarityMap.entries());
    self.postMessage(a);
};
