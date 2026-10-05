import type { AnswerKeyword } from "../api/types";

/**
 * The keywords a message can start with, offered while its first word is
 * being typed.
 *
 * A keyword only counts as the first word (`@pheasant` anywhere), which is
 * also the only moment it is worth suggesting: once a space follows the
 * first word, the menu goes away and stays away. The list is the region's
 * own (`/assistant/status` → `keywords`), so a deployment that turned the
 * keywords or the index answers off does not advertise them.
 */
export function KeywordMenu({
  draft,
  keywords,
  onPick,
}: {
  draft: string;
  keywords: AnswerKeyword[];
  onPick: (text: string) => void;
}) {
  const matches = keywordMatches(draft, keywords);
  if (matches.length === 0) return null;
  const groups = new Map<string, AnswerKeyword[]>();
  for (const keyword of matches) {
    groups.set(keyword.group, [...(groups.get(keyword.group) ?? []), keyword]);
  }
  return (
    <div className="keyword-menu" role="listbox" aria-label="Keywords">
      {[...groups.entries()].map(([group, members]) => (
        <div className="keyword-menu__group" key={group}>
          <div className="keyword-menu__title">{group}</div>
          {members.map((keyword) => (
            <button
              type="button"
              role="option"
              aria-selected={false}
              className="keyword-menu__item"
              key={keyword.keyword}
              onMouseDown={(event) => event.preventDefault()}
              onClick={() => onPick(`${keyword.keyword} `)}
              title={`For example: ${keyword.example}`}
            >
              <code>{keyword.keyword}</code>
              <span>{keyword.summary}</span>
            </button>
          ))}
        </div>
      ))}
    </div>
  );
}

/** The keywords the draft's first word could become, while it is being typed. */
export function keywordMatches(draft: string, keywords: AnswerKeyword[]): AnswerKeyword[] {
  const typing = /^\s*@([\w-]*)$/.exec(draft);
  if (!typing) return [];
  const prefix = typing[1].toLowerCase();
  return keywords.filter((keyword) =>
    [keyword.keyword, ...keyword.aliases].some((name) => name.slice(1).startsWith(prefix)),
  );
}
