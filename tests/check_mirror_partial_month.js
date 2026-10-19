/* Exercise the SHIPPED charts.js partial-month caption against the real payload.
 * The functions are lifted out of site/charts.js by name (brace-matched), not
 * re-implemented, so this tests the code that deploy_pages.sh copies.
 *
 *   node tests/check_mirror_partial_month.js
 */
const fs = require('fs');
const path = require('path');

const src = fs.readFileSync(path.join(__dirname, '..', 'site', 'charts.js'), 'utf8');

function grab(name, prefix) {
  const re = new RegExp((prefix || 'function ') + name + '\\s*\\(', 'g');
  const m = re.exec(src);
  if (!m) throw new Error('not found: ' + name);
  let i = src.indexOf('{', m.index + m[0].length - 1);
  let depth = 0;
  for (let j = i; j < src.length; j++) {
    if (src[j] === '{') depth++;
    else if (src[j] === '}') { depth--; if (depth === 0) return src.slice(m.index, j + 1); }
  }
  throw new Error('unbalanced: ' + name);
}

function grabVar(name) {
  const m = new RegExp('var ' + name + '\\s*=\\s*([^;]+);').exec(src);
  if (!m) throw new Error('var not found: ' + name);
  return 'var ' + name + ' = ' + m[1] + ';';
}

const names = ['monthKeyOf', 'monthLabel', 'monthIndex', 'monthFromIndex', 'monthRange',
               'selectMonths', 'daysInMonth', 'ordOf', 'partialNote'];
const code = [grabVar('MONTHS'), grabVar('MAX_MONTHS')]
  .concat(names.map(n => grab(n))).join('\n');
const api = new Function(code + '\nreturn {' + names.join(',') + '};')();

const data = JSON.parse(fs.readFileSync(path.join(__dirname, '..', 'site', 'data', 'reviews.json'), 'utf8'));
const rows = data.rows;

const sel = api.selectMonths(rows);
const note = api.partialNote(rows, sel.months);
const maxD = rows.map(r => r.d).sort().pop();

console.log('months :', sel.months.join(' '));
console.log('max row:', maxD);
console.log('note   :', note || '(none)');
console.log('totals :', sel.months.map(m => m + '=' + (sel.totals[m] || 0)).join(' '));

const wantMonth = maxD.slice(0, 7);
const fails = [];
if (sel.months[sel.months.length - 1] !== wantMonth) fails.push('last month is not the data month');
if (!note) fails.push('no partial-month note');
if (!note.includes('is partial')) fails.push('note does not say partial');
if (!note.includes('data to the ' + api.ordOf(parseInt(maxD.slice(8, 10), 10)))) fails.push('note does not give the real end day');
if (!note.includes(maxD)) fails.push('note does not print the last row date');
if (!/short month, not a fall/.test(note)) fails.push('note does not warn off the fall reading');

// a complete final month must NOT produce a note
const complete = rows.filter(r => r.d && r.d < '2026-10-01');
const n2 = api.partialNote(complete, api.selectMonths(complete).months);
if (n2) fails.push('note printed for a complete final month: ' + n2);

console.log(fails.length ? 'FAIL ' + fails.join('; ') : 'ok  mirror caption describes the partial month');
process.exit(fails.length ? 1 : 0);
