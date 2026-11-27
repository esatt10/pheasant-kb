/** What a resolver is, in words a person uses. */
export const PLACE_KINDS: Record<string, { label: string; reads: string }> = {
  pheasant: { label: "Indexed source", reads: "Pheasant's index of a source, as it is now" },
  folder: { label: "Folder", reads: "a folder on the server, file by file" },
  git: { label: "Git repository", reads: "a repository at a pinned commit" },
  connection: { label: "Connected account", reads: "GitHub, Drive or SharePoint, under its policy" },
  attachment: { label: "Attached files", reads: "files someone attached, fixed as they were" },
};

export const OPERATIONS: Record<string, string> = {
  resolve: "pin a version",
  enumerate: "list files",
  grep: "search text",
  read: "read files",
  inspect: "inspect",
  materialize: "copy into a workspace",
  compare: "compare versions",
};

