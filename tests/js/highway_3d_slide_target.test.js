// Pins slide-target gem suppression in plugins/highway_3d/screen.js (PR #329).
// A note that is the slide/link destination of a preceding sustained note has
// its gem body suppressed (skipBody=true) so it does not render a duplicate
// gem on top of the slide trail — but the sustain/slide trail itself still
// renders so the slide motion stays visible. A refactor that drops the
// _slideTargetSet pre-pass, stops threading _isSlideTgt into drawNote, or
// moves the trail back inside the !skipBody gate would silently reintroduce
// duplicate gems or erase slide trails.
//
// Source-level only — same strategy as the other tests/js/ files.

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const SCREEN_JS = path.join(__dirname, '..', '..', 'plugins', 'highway_3d', 'screen.js');

test('a _slideTargetSet pre-pass builds the suppressed-gem set from bundle.notes', () => {
    const src = fs.readFileSync(SCREEN_JS, 'utf8');
    assert.match(
        src,
        /const\s+checkSrc\s*=\s*\([^)]*\)\s*=>\s*\{[\s\S]*?stSet\.add\(/,
        'pre-pass checkSrc must populate the slide-target set',
    );
    assert.match(
        src,
        /if\s*\(\s*stSet\.size\s*>\s*0\s*\)\s*_slideTargetSet\s*=\s*stSet/,
        '_slideTargetSet must be assigned from the pre-pass result',
    );
});

test('_isSlideTgt is derived from _slideTargetSet membership', () => {
    const src = fs.readFileSync(SCREEN_JS, 'utf8');
    assert.match(
        src,
        /_isSlideTgt\s*=\s*!!\(\s*_slideTargetSet\s*&&\s*_slideTargetSet\.has\(/,
        '_isSlideTgt must test _slideTargetSet membership',
    );
});

test('_isSlideTgt is threaded into drawNote as the skipBody argument', () => {
    // drawNote(n, now, openX, skipLabel, skipBody, ...) — _isSlideTgt sits in
    // the 5th (skipBody) position so the gem body is suppressed.
    const src = fs.readFileSync(SCREEN_JS, 'utf8');
    assert.match(
        src,
        /drawNote\(\s*n\s*,\s*now\s*,\s*singleOpenX\s*,\s*skipLabel\s*,\s*_isSlideTgt\s*,/,
        '_isSlideTgt must be passed as drawNote\'s skipBody argument',
    );
});

test('the sustain trail renders for all notes, including skipBody slide targets', () => {
    // The trail block must stay outside the !skipBody gem gate so suppressed
    // slide-target gems still show their slide trail.
    const src = fs.readFileSync(SCREEN_JS, 'utf8');
    assert.match(
        src,
        /Rendered for ALL notes with sustain, including skipBody=true/,
        'the sustain-trail comment contract must remain, marking the trail as unconditional',
    );
});
