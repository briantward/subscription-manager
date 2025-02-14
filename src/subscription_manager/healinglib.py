# Copyright (c) 2013 Red Hat, Inc.
#
# This software is licensed to you under the GNU General Public License,
# version 2 (GPLv2). There is NO WARRANTY for this software, express or
# implied, including the implied warranties of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE. You should have received a copy of GPLv2
# along with this software; if not, see
# http://www.gnu.org/licenses/old-licenses/gpl-2.0.txt.
#
# Red Hat trademarks are not licensed under GPLv2. No permission is
# granted to use or replicate Red Hat trademarks that are incorporated
# in this software or its documentation.
#

import datetime
import logging
from typing import List, TYPE_CHECKING

from rhsm import certificate

from subscription_manager import certlib
from subscription_manager import entcertlib
from subscription_manager import injection as inj

if TYPE_CHECKING:
    from subscription_manager.cert_sorter import CertSorter
    from subscription_manager.identity import Identity
    from subscription_manager.plugins import PluginManager
    from rhsm.connection import UEPConnection
    from subscription_manager.cp_provider import CPProvider

log = logging.getLogger(__name__)


class HealingActionInvoker(certlib.BaseActionInvoker):
    """
    An object used to run healing nightly. Checks cert validity for today, heals
    if necessary, then checks for 24 hours from now, so we theoretically will
    never have invalid certificates if subscriptions are available.

    NOTE: We may update entitlement status in this class, but we do not
          update entitlement certs, since we are inside a lock. So a
          EntCertActionInvoker.update() needs to follow a HealingActionInvoker.update()
    """

    def _do_update(self) -> int:
        action = HealingUpdateAction()
        return action.perform()


class HealingUpdateAction:
    """UpdateAction for ent cert healing.

    Core if entitlement certificate healing.

    Asks RHSM API to calculate entitlement status today, and tomorrow.
    If either show incomplete entitlement, ask the RHSM API to
    auto attach pools to fix entitlement.

    Attempts to avoid gaps in entitlement coverage.

    Used by subscription-manager if the "autoheal" options
    are enabled.

    Returns an EntCertUpdateReport with information about any ent
    certs that were changed.

    Plugin hooks:
        pre_auto_attach
        post_auto_attach
    """

    def __init__(self):
        self.cp_provider: CPProvider = inj.require(inj.CP_PROVIDER)
        self.uep: UEPConnection = self.cp_provider.get_consumer_auth_cp()
        self.report: entcertlib.EntCertUpdateReport = entcertlib.EntCertUpdateReport()
        self.plugin_manager: PluginManager = inj.require(inj.PLUGIN_MANAGER)

    def perform(self):
        # inject
        identity: Identity = inj.require(inj.IDENTITY)
        uuid: str = identity.uuid
        consumer: dict = self.uep.getConsumer(uuid)

        if "autoheal" not in consumer or not consumer["autoheal"]:
            log.warning("Auto-heal disabled on server, skipping.")
            return 0

        try:

            today: datetime.datetime = datetime.datetime.now(certificate.GMT())
            tomorrow: datetime.datetime = today + datetime.timedelta(days=1)
            valid_today: bool = False
            valid_tomorrow: bool = False

            # Check if we're invalid today and heal if so. If we are
            # valid, see if 24h from now is greater than our "valid until"
            # date, and heal for tomorrow if so.

            cs: CertSorter = inj.require(inj.CERT_SORTER)

            cert_updater = entcertlib.EntCertActionInvoker()
            if not cs.is_valid():
                log.warning("Found invalid entitlements for today: %s" % today)
                self.plugin_manager.run("pre_auto_attach", consumer_uuid=uuid)
                ents: List[dict] = self.uep.bind(uuid, today)
                self.plugin_manager.run("post_auto_attach", consumer_uuid=uuid, entitlement_data=ents)

                # NOTE: we need to call EntCertActionInvoker.update after Healing.update
                # otherwise, the locking get's crazy
                # hmm, we use RLock, maybe we could use it here
                self.report = cert_updater.update()
            else:
                valid_today = True

                if cs.compliant_until is None:
                    # Edge case here, not even sure this can happen as we
                    # should have a compliant until date if we're valid
                    # today, but just in case:
                    log.warning("Got valid status from server but no valid until date.")
                elif tomorrow > cs.compliant_until:
                    log.warning("Entitlements will be invalid by tomorrow: %s" % tomorrow)
                    self.plugin_manager.run("pre_auto_attach", consumer_uuid=uuid)
                    ents = self.uep.bind(uuid, tomorrow)
                    self.plugin_manager.run("post_auto_attach", consumer_uuid=uuid, entitlement_data=ents)
                    self.report = cert_updater.update()
                else:
                    valid_tomorrow = True

            msg = "Entitlement auto healing was checked and entitlements"
            if valid_today:
                msg += " are valid today %s" % today
                if valid_tomorrow:
                    msg += " and tomorrow %s" % tomorrow
            log.debug(msg)

        except Exception as e:
            log.error("Error attempting to auto-heal:")
            log.exception(e)
            self.report._exceptions.append(e)
            return self.report
        else:
            log.debug("Auto-heal check complete.")
            return self.report
