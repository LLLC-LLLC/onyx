"use client";

// Bottom-left floating banner: shows one banner-worthy notification at a time
// (admin site-wide announcement, license expiry warning, trial-ending notice),
// pageable via prev/next when more than one is active. Always dismissible.

import { usePathname } from "next/navigation";
import { MessageCard, Button, Text } from "@opal/components";
import { markdown } from "@opal/utils";
import { timeAgo } from "@opal/time";
import { SvgChevronLeft, SvgChevronRight, SvgX } from "@opal/icons";
import type { StatusVariants } from "@opal/types";
import { isAuthPath } from "@/lib/auth/paths";
import { getNotificationIcon } from "@/lib/notifications";
import {
  NotificationType,
  type Notification,
} from "@/lib/notifications/interfaces";
import {
  LICENSE_EXPIRY_ERROR_THRESHOLD,
  licenseExpirySeverity,
  useBannerQueue,
} from "@/lib/banner/hooks";

function bannerVariant(notification: Notification): StatusVariants {
  switch (notification.notif_type) {
    case NotificationType.SYSTEM_ANNOUNCEMENT:
      return "info";
    case NotificationType.TRIAL_ENDS_TWO_DAYS:
      return "warning";
    case NotificationType.LICENSE_EXPIRY_WARNING:
      return licenseExpirySeverity(notification) >=
        LICENSE_EXPIRY_ERROR_THRESHOLD
        ? "error"
        : "warning";
    default:
      return "default";
  }
}

function bannerSourceLabel(notifType: NotificationType): string {
  switch (notifType) {
    case NotificationType.SYSTEM_ANNOUNCEMENT:
      return "Admin announcement";
    case NotificationType.LICENSE_EXPIRY_WARNING:
      return "License";
    case NotificationType.TRIAL_ENDS_TWO_DAYS:
      return "Trial";
    default:
      return "Notification";
  }
}

export default function BannerQueue() {
  const pathname = usePathname();
  const { current, hasMultiple, goToNext, goToPrevious, dismissCurrent } =
    useBannerQueue();

  if (isAuthPath(pathname) || !current) return null;

  const relativeTime = timeAgo(current.last_shown);
  const footer = relativeTime
    ? `${bannerSourceLabel(current.notif_type)} • ${relativeTime}`
    : bannerSourceLabel(current.notif_type);

  return (
    <div className="fixed bottom-[76px] left-3 z-toast w-[336px] max-w-[calc(100vw-1.5rem)]">
      <MessageCard
        variant={bannerVariant(current)}
        icon={getNotificationIcon(current.notif_type)}
        title={current.title}
        rightChildren={
          <div className="flex items-center gap-0.5">
            {hasMultiple && (
              <>
                <Button
                  icon={SvgChevronLeft}
                  prominence="internal"
                  size="md"
                  onClick={goToPrevious}
                  aria-label="Previous banner"
                />
                <Button
                  icon={SvgChevronRight}
                  prominence="internal"
                  size="md"
                  onClick={goToNext}
                  aria-label="Next banner"
                />
              </>
            )}
            <Button
              icon={SvgX}
              prominence="internal"
              size="md"
              onClick={() => void dismissCurrent()}
              aria-label="Dismiss"
            />
          </div>
        }
        bottomChildren={
          <div className="flex flex-col gap-1 px-2 pb-2">
            {current.description && (
              <Text font="main-ui-body" color="text-03">
                {markdown(current.description)}
              </Text>
            )}
            <Text font="secondary-body" color="text-02">
              {footer}
            </Text>
          </div>
        }
      />
    </div>
  );
}
