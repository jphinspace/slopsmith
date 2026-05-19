// Pins the sustain bloom glow in plugins/highway_3d/screen.js (PR #329).
// Sustained chord rails get a soft gaussian glow: a DataTexture gaussian
// (_makeGaussTex) drives a wider, additive-blended plane mesh (pSusRailBloom)
// rendered behind the core rail. A refactor that drops the gaussian texture,
// stops using additive blending, or bumps the bloom renderOrder above the
// core rail (16) would silently regress or invert the effect.
//
// Source-level only — same strategy as the other tests/js/ files.

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const SCREEN_JS = path.join(__dirname, '..', '..', 'plugins', 'highway_3d', 'screen.js');

test('a gaussian DataTexture helper (_makeGaussTex) drives the bloom falloff', () => {
    const src = fs.readFileSync(SCREEN_JS, 'utf8');
    assert.match(
        src,
        /function\s+_makeGaussTex\s*\(/,
        '_makeGaussTex must exist to build the bloom gaussian texture',
    );
    assert.match(
        src,
        /_bloomGaussTex\s*=\s*_makeGaussTex\(/,
        'the bloom texture must be produced by _makeGaussTex',
    );
});

test('the bloom rail material uses additive blending', () => {
    const src = fs.readFileSync(SCREEN_JS, 'utf8');
    assert.match(
        src,
        /mSusRailBloomBase\s*=\s*new\s+T\.MeshBasicMaterial\(\{[\s\S]*?blending:\s*T\.AdditiveBlending[\s\S]*?\}\)/,
        'mSusRailBloomBase must blend additively so it brightens what is behind it',
    );
});

test('the bloom pool seeds meshes at renderOrder 14, behind the core rail (16)', () => {
    // renderOrder 14 keeps the bloom behind the core sustain rail (16) so the
    // glow reads as a trail rather than occluding the rail.
    const src = fs.readFileSync(SCREEN_JS, 'utf8');
    assert.match(
        src,
        /pSusRailBloom\s*=\s*pool\([^)]*,\s*\(\)\s*=>\s*\{[\s\S]*?m\.renderOrder\s*=\s*14\s*;[\s\S]*?\}\s*\)/,
        'pSusRailBloom pool must seed meshes with renderOrder = 14',
    );
});
