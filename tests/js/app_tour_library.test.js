const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const tourScript = path.join(__dirname, '..', '..', 'plugins', 'app_tour_library', 'script.js');
const SRC = fs.readFileSync(tourScript, 'utf8');

function extractBlock(src, signature) {
    const start = src.indexOf(signature);
    assert.ok(start !== -1, `signature '${signature}' not found`);
    const openBrace = src.indexOf('{', start);
    assert.ok(openBrace !== -1, `opening brace after '${signature}' not found`);
    let depth = 1;
    let i = openBrace + 1;
    while (i < src.length && depth > 0) {
        const ch = src[i];
        if (ch === '{') depth++;
        else if (ch === '}') depth--;
        i++;
    }
    assert.ok(depth === 0, `unbalanced braces after '${signature}'`);
    return src.slice(start, i);
}

test('library tour registers dynamic steps for provider-aware instructions', () => {
    const register = extractBlock(SRC, 'function _register()');
    assert.match(register, /buildSteps:\s*_buildSteps/,
        'Library tour registration must use buildSteps so provider instructions can be conditional');
});

test('library tour adds provider step only when multiple browsable providers exist', () => {
    const providerCheck = extractBlock(SRC, 'async function _hasMultipleProviders()');
    assert.match(providerCheck, /fetch\(\s*['"]\/api\/library\/providers['"]\s*\)/,
        'Provider count must come from the library providers endpoint');
    assert.match(providerCheck, /filter\(_isBrowsableProvider\)/,
        'Provider count must ignore non-browsable providers');
    assert.match(providerCheck, /providers\.length\s*>\s*1/,
        'Provider step must require more than one provider');

    const buildSteps = extractBlock(SRC, 'async function _buildSteps()');
    assert.match(buildSteps, /!\(await\s+_hasMultipleProviders\(\)\)/,
        'buildSteps must skip the provider step unless multiple providers are present');
    assert.match(buildSteps, /splice\([\s\S]*PROVIDER_STEP/,
        'buildSteps must insert the provider instruction into the tour');
});

test('library tour inserts provider step after the search step', () => {
    const buildSteps = extractBlock(SRC, 'async function _buildSteps()');
    assert.match(buildSteps, /insertAt\s*===\s*-1\s*\?\s*1\s*:\s*insertAt\s*\+\s*1/,
        'Provider step must be inserted after the search step when search is found');
    assert.match(buildSteps, /splice\([\s\S]*PROVIDER_STEP/,
        'buildSteps must splice PROVIDER_STEP into the tour');
});

test('provider tour step targets the library provider selector', () => {
    assert.match(SRC, /id:\s*['"]library-provider['"]/,
        'Provider step must have a stable id');
    assert.match(SRC, /selector:\s*['"]#lib-provider['"]/,
        'Provider step must spotlight the provider selector');
    assert.match(SRC, /waitFor:\s*['"]#lib-provider['"]/,
        'Provider step must wait for the selector before showing');
});
