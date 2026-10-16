
export const INSPECT_SECTION_ORDER = {
  page: 100,
  shells: 200,
  liveness: 300,
  configOverlay: 400,
  cost: 500,
  activity: 600,
  runLink: 700,
  notice: 800,
} as const;

export function fleetTaskHref(id: number): `/fleet?task=${number}` {
  return `/fleet?task=${id}`;
}

export function fleetNoticeHref(id: number): `/fleet?notice=${number}` {
  return `/fleet?notice=${id}`;
}
