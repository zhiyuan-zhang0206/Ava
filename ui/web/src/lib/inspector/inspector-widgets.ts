// meiva:ref ui/web/src/docs/frontend-components/inspector/inspector.meiva.okf.md#section-order
export const INSPECT_SECTION_ORDER = {
  runLink: 50,
  costActivity: 75,
  page: 100,
  shells: 200,
  liveness: 300,
  configOverlay: 400,
  notice: 800,
} as const;

export function fleetTaskHref(id: number): `/fleet?task=${number}` {
  return `/fleet?task=${id}`;
}

export function fleetNoticeHref(id: number): `/fleet?notice=${number}` {
  return `/fleet?notice=${id}`;
}
