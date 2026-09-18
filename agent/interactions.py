"""Conservative terminal prompt hints; decisions always require a user's click."""
import hashlib
import re

KEYS = frozenset(('Up', 'Down', 'Left', 'Right', 'Enter', 'Escape', 'Tab', 'Space', 'BSpace', 'C-c'))
OPTION = re.compile(r'^\s*([›❯>●])?\s*(\d{1,2})[.)]\s+(.+?)\s*$')
QUESTION = re.compile(
    r'would you like|do you (?:want|trust)|proceed|allow|approve|permission|'
    r'trust (?:this|the)|select|choose|which|是否|请选择|允许|同意|信任|继续', re.I)


def screen_id(screen):
    return hashlib.sha256(screen.encode()).hexdigest()


def detect_interaction(screen):
    # Only inspect the visible pane, never a historical scrollback approval.
    lines = [line.strip().strip('│').strip() for line in screen.splitlines()]
    while lines and not lines[-1]:
        lines.pop()
    lines = lines[-32:]
    matches = [(i, OPTION.match(line)) for i, line in enumerate(lines)]
    matches = [(i, m) for i, m in matches if m]
    if 2 <= len(matches) <= 9:
        first, last = matches[0][0], matches[-1][0]
        context = '\n'.join(lines[max(0, first - 10):])
        selected = [n for n, (_, m) in enumerate(matches) if m[1]]
        # A selected marker plus a question distinguishes menus from most prose.
        # Leave unfamiliar layouts to the explicit terminal key controls.
        between = lines[first:last + 1]
        if (len(selected) == 1 and QUESTION.search(context)
                and all(not line or OPTION.match(line) for line in between)
                and len(lines) - last <= 6
                and len({m[2] for _, m in matches}) == len(matches)):
            return {'kind': 'choice', 'text': context[-2000:],
                    'options': [{'id': m[2], 'label': m[3][:120], 'selected': n == selected[0]}
                                for n, (_, m) in enumerate(matches)]}
    tail = '\n'.join(lines[-6:])
    if re.search(r'\([yY]/[nN]\)|\[[yY]/[nN]\]|\[yes/no\]|\(yes/no\)', tail):
        return {'kind': 'confirm', 'text': tail[-2000:],
                'options': [{'id': 'yes', 'label': '同意 / Yes'}, {'id': 'no', 'label': '拒绝 / No'}]}
    if re.search(r'press enter to (?:continue|confirm)|按\s*(?:Enter|回车)\s*(?:继续|确认)', tail, re.I):
        return {'kind': 'continue', 'text': tail[-2000:],
                'options': [{'id': 'continue', 'label': '继续 / Enter'}]}
    return None


def choice_keys(interaction, choice):
    option = next((o for o in interaction['options'] if o['id'] == choice), None)
    if option is None:
        raise ValueError('Invalid choice')
    if interaction['kind'] == 'choice':
        selected = next(i for i, o in enumerate(interaction['options']) if o['selected'])
        target = interaction['options'].index(option)
        return (['Down'] * (target - selected) if target > selected else ['Up'] * (selected - target)) + ['Enter']
    if interaction['kind'] == 'confirm':
        # send-keys interprets these literal letters, never arbitrary key names.
        answer = ('yes' if choice == 'yes' else 'no') if 'yes/no' in interaction['text'] else ('y' if choice == 'yes' else 'n')
        return [answer, 'Enter']
    return ['Enter']
