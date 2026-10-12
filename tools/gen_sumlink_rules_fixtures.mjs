// Generates tests/fixtures/sumlink_rules.json by running SumLink's real JS
// rules (src/utils/gameLogic.js) on random boards. The Python port is then
// tested against this output.
//
// Usage: node tools/gen_sumlink_rules_fixtures.mjs <path-to-gameLogic.js> [boards=600]
import { readFileSync, writeFileSync, mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, dirname } from 'node:path';
import { pathToFileURL, fileURLToPath } from 'node:url';

const [, , srcPath, countArg] = process.argv;
if (!srcPath) {
  console.error('usage: node tools/gen_sumlink_rules_fixtures.mjs <gameLogic.js> [boards]');
  process.exit(1);
}
// gameLogic.js is an ES module inside a CommonJS package, so copy it to a .mjs to import it.
const tmp = join(mkdtempSync(join(tmpdir(), 'sumlink-')), 'gameLogic.mjs');
writeFileSync(tmp, readFileSync(srcPath, 'utf8'));
const logic = await import(pathToFileURL(tmp).href);

// Deterministic PRNG so the fixture file is reproducible.
let seed = 12345;
const rand = () => ((seed = (seed * 1103515245 + 12345) % 2147483648) / 2147483648);

function randomBoard() {
  const board = Array.from({ length: 108 }, () => ({ value: null, state: 'cleared' }));
  const filled = Math.floor(rand() * 109);
  const activeRatio = 0.3 + rand() * 0.7;
  for (let i = 0; i < filled; i++) {
    board[i] = { value: 1 + Math.floor(rand() * 9), state: rand() < activeRatio ? 'active' : 'blurred' };
  }
  return board;
}

const boards = [];
const total = Number(countArg || 600);
for (let b = 0; b < total; b++) {
  const board = b === 0 ? logic.generateSeededGrid(1) : randomBoard();
  const active = [];
  board.forEach((c, i) => c.state === 'active' && active.push(i));
  let checks = [];
  for (let x = 0; x < active.length; x++) {
    for (let y = x + 1; y < active.length; y++) {
      // Check both orders; the JS is not obviously symmetric.
      for (const [i, j] of [[active[x], active[y]], [active[y], active[x]]]) {
        const r = logic.isValidPairFlat(board, i, j, 9);
        checks.push([i, j, r.valid, r.type ?? null, r.blocking]);
      }
    }
  }
  // Keep the fixture small: every valid pair plus a random sample of the rest.
  const valid = checks.filter(c => c[2]);
  const invalid = checks.filter(c => !c[2]).filter(() => rand() < 0.08);
  checks = [...valid, ...invalid];
  const firstPair = logic.findValidPairFlat(board, 9);
  let afterMatch = null;
  if (firstPair) {
    // Same steps as processMatch: blur both cells, then compact.
    const b = board.map(c => ({ ...c }));
    b[firstPair[0]] = { ...b[firstPair[0]], state: 'blurred' };
    b[firstPair[1]] = { ...b[firstPair[1]], state: 'blurred' };
    const { newBoard, removedRows } = logic.compactBoard(b);
    afterMatch = { board: newBoard, removed_rows: removedRows };
  }
  boards.push({
    board,
    after_first_match: afterMatch,
    add_numbers: logic.addNumbers(board),
    first_pair: firstPair,
    all_pairs: logic.findValidPairsFlat(board, 9),
    cleared: logic.isGameCompleteFlat(board),
    checks,
  });
}

const out = join(dirname(fileURLToPath(import.meta.url)), '..', 'tests', 'fixtures', 'sumlink_rules.json');
writeFileSync(out, JSON.stringify({ source: srcPath, boards }));
console.log(`Wrote ${boards.length} boards, ${boards.reduce((n, b) => n + b.checks.length, 0)} pair checks to ${out}`);
