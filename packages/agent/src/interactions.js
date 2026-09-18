import { createHash } from 'node:crypto';
import { tail } from './utils.js';

export const KEYS = new Set(['Up', 'Down', 'Left', 'Right', 'Enter', 'Escape', 'Tab', 'Space', 'BSpace', 'C-c']);
const OPTION = /^\s*([›❯>●])?\s*(\d{1,2})[.)]\s+(.+?)\s*$/u;
const QUESTION = /would you like|do you (?:want|trust)|proceed|allow|approve|permission|trust (?:this|the)|select|choose|which|是否|请选择|允许|同意|信任|继续/i;
export const screenId = screen => createHash('sha256').update(screen).digest('hex');

export function detectInteraction(screen) {
  let lines = screen.split(/\r?\n/u).map(line => line.trim().replace(/^│+|│+$/gu, '').trim());
  while (lines.length && !lines.at(-1)) lines.pop();
  lines = lines.slice(-32);
  const matches = lines.map((line, index) => ({ index, match: OPTION.exec(line) })).filter(item => item.match);
  if (matches.length >= 2 && matches.length <= 9) {
    const first = matches[0].index, last = matches.at(-1).index;
    const context = lines.slice(Math.max(0, first - 10)).join('\n');
    const selected = matches.flatMap((item, index) => item.match[1] ? [index] : []);
    if (selected.length === 1 && QUESTION.test(context) && lines.length - last <= 6
        && lines.slice(first, last + 1).every(line => !line || OPTION.test(line))
        && new Set(matches.map(item => item.match[2])).size === matches.length) {
      return { kind: 'choice', text: tail(context, 2000), options: matches.map(({ match }, index) => ({
        id: match[2], label: Array.from(match[3]).slice(0, 120).join(''), selected: index === selected[0]
      })) };
    }
  }
  const end = lines.slice(-6).join('\n');
  if (/\([yY]\/[nN]\)|\[[yY]\/[nN]\]|\[yes\/no\]|\(yes\/no\)/u.test(end)) {
    return { kind: 'confirm', text: tail(end, 2000), options: [{ id: 'yes', label: '同意 / Yes' }, { id: 'no', label: '拒绝 / No' }] };
  }
  if (/press enter to (?:continue|confirm)|按\s*(?:Enter|回车)\s*(?:继续|确认)/iu.test(end)) {
    return { kind: 'continue', text: tail(end, 2000), options: [{ id: 'continue', label: '继续 / Enter' }] };
  }
  return null;
}

export function choiceKeys(interaction, choice) {
  const index = interaction.options.findIndex(option => option.id === choice);
  if (index < 0) throw Error('Invalid choice');
  if (interaction.kind === 'choice') {
    const delta = index - interaction.options.findIndex(option => option.selected);
    return [...Array(Math.abs(delta)).fill(delta > 0 ? 'Down' : 'Up'), 'Enter'];
  }
  if (interaction.kind === 'confirm') {
    const answer = interaction.text.includes('yes/no') ? (choice === 'yes' ? 'yes' : 'no') : (choice === 'yes' ? 'y' : 'n');
    return [answer, 'Enter'];
  }
  return ['Enter'];
}
