// Pins Wide-ahead camera bounds so stale/high chart anchors cannot keep a
// low-fret passage zoomed out when actual fretted notes are present.

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const SCREEN_JS = path.join(__dirname, '..', '..', 'plugins', 'highway_3d', 'screen.js');

function screenSource() {
    return fs.readFileSync(SCREEN_JS, 'utf8');
}

test('3D camera modes share actual fretted note/chord bounds before anchor fallback', () => {
    const src = screenSource();
    const fnStart = src.indexOf('function computeCameraFretBounds');
    assert.notEqual(fnStart, -1, 'computeCameraFretBounds must exist');
    const fnEnd = src.indexOf('\n        function lookaheadTargetWorldX', fnStart);
    assert.notEqual(fnEnd, -1, 'lookaheadTargetWorldX anchor must follow shared camera bounds helper');
    const fn = src.slice(fnStart, fnEnd);

    assert.match(fn, /let\s+noteMinF\s*=\s*99/);
    assert.match(fn, /let\s+anchorMinF\s*=\s*99/);
    assert.match(
        fn,
        /if\s*\(\s*noteAny\s*&&\s*noteMinF\s*<=\s*noteMaxF\s*\)\s*return\s*\{\s*minF:\s*noteMinF,\s*maxF:\s*noteMaxF\s*\}/,
        'fretted note/chord bounds should be returned before anchor fallback',
    );
    assert.match(
        fn,
        /if\s*\(\s*anchorAny\s*&&\s*anchorMinF\s*<=\s*anchorMaxF\s*\)\s*return\s*\{\s*minF:\s*anchorMinF,\s*maxF:\s*anchorMaxF\s*\}/,
        'anchors should remain a fallback for windows with no fretted events',
    );

    assert.match(
        src,
        /const\s+sharedCameraBoundsNow\s*=\s*\n\s*computeCameraFretBounds\(now,\s*anchors,\s*notes,\s*chords,\s*CAM_LOOKAHEAD_SEC\)/,
        'camera bounds should be computed once from the shared helper',
    );
    assert.match(
        src,
        /const\s+steadyBoundsNow\s*=\s*\(cameraMode\s*===\s*'lookahead'\)\s*\?\s*null\s*:\s*sharedCameraBoundsNow/,
        'steady mode should use the same shared camera bounds as lookahead mode',
    );
});
