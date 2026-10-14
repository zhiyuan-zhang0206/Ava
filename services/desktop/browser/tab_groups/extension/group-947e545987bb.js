// Ava tab-group worker: files one CDP target into the tab group titled
// `title`, creating the group on first use and coalescing any duplicate open
// groups with the same title into the first one. Stateless on purpose -- the
// group is found by its title, so no groupId mapping has to survive a daemon
// reconnect or a Chrome restart. The caller guarantees the title is unique per
// agent. Colors are left to Chrome.
//
// This file's NAME carries a hash of its content (see tab_groups/grouping.py):
// Chrome caches an extension's service-worker script in the profile and keeps
// running the cached copy when only the content changes.
self.avaGroup = async (targetId, title) => {
  const target = (await chrome.debugger.getTargets()).find((t) => t.id === targetId);
  if (!target || target.tabId === undefined) {
    throw new Error(`no tab for target ${targetId}`);
  }
  const [existing, ...duplicates] = await chrome.tabGroups.query({ title });
  if (existing) {
    const extraTabs = (
      await Promise.all(duplicates.map((g) => chrome.tabs.query({ groupId: g.id })))
    ).flat();
    const tabIds = [
      target.tabId,
      ...extraTabs.map((t) => t.id).filter((id) => id !== undefined && id !== target.tabId),
    ];
    return chrome.tabs.group({ groupId: existing.id, tabIds });
  }
  const groupId = await chrome.tabs.group({ tabIds: [target.tabId] });
  await chrome.tabGroups.update(groupId, { title });
  return groupId;
};
