# -*- coding: utf-8 -*-
# ------------------------------------------------------------------------------
#
#   Copyright 2018-2020 Fetch.AI Limited
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.
#
# ------------------------------------------------------------------------------

"""This module contains utilities for building an AEA."""
import itertools
import logging
import os
import types
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Collection, Dict, List, Optional, Set, Tuple, Union, cast

import jsonschema

from aea import AEA_DIR
from aea.aea import AEA
from aea.configurations.base import (
    AgentConfig,
    ComponentConfiguration,
    ComponentId,
    ComponentType,
    ConfigurationType,
    DEFAULT_AEA_CONFIG_FILE,
    Dependencies,
    PublicId,
)
from aea.configurations.components import Component
from aea.configurations.loader import ConfigLoader
from aea.connections.base import Connection
from aea.context.base import AgentContext
from aea.crypto.ethereum import ETHEREUM
from aea.crypto.fetchai import FETCHAI
from aea.crypto.helpers import (
    ETHEREUM_PRIVATE_KEY_FILE,
    FETCHAI_PRIVATE_KEY_FILE,
    _create_ethereum_private_key,
    _create_fetchai_private_key,
    _try_validate_ethereum_private_key_path,
    _try_validate_fet_private_key_path,
)
from aea.crypto.ledger_apis import LedgerApis
from aea.crypto.wallet import SUPPORTED_CRYPTOS, Wallet
from aea.helpers.base import _SysModules
from aea.identity.base import Identity
from aea.protocols.base import Protocol
from aea.registries.resources import Resources
from aea.skills.base import Skill

PathLike = Union[os.PathLike, Path, str]

logger = logging.getLogger(__name__)


class _DependenciesManager:
    """Class to manage dependencies of agent packages."""

    def __init__(self):
        """Initialize the dependency graph."""
        # adjacency list of the dependency DAG
        # an arc means "depends on"
        self._dependencies = {}  # type: Dict[ComponentId, Component]
        self._all_dependencies_by_type = (
            {}
        )  # type: Dict[ComponentType, Dict[ComponentId, Component]]
        self._prefix_to_components = (
            {}
        )  # type: Dict[Tuple[ComponentType, str, str], Set[ComponentId]]
        self._inverse_dependency_graph = {}  # type: Dict[ComponentId, Set[ComponentId]]

    @property
    def all_dependencies(self) -> Set[ComponentId]:
        """Get all dependencies."""
        result = set(self._dependencies.keys())
        return result

    @property
    def dependencies_highest_version(self) -> Set[ComponentId]:
        """Get the dependencies with highest version."""
        return {max(ids) for _, ids in self._prefix_to_components.items()}

    @property
    def protocols(self) -> Dict[ComponentId, Protocol]:
        """Get the protocols."""
        return cast(
            Dict[ComponentId, Protocol],
            self._all_dependencies_by_type.get(ComponentType.PROTOCOL, {}),
        )

    @property
    def connections(self) -> Dict[ComponentId, Connection]:
        """Get the connections."""
        return cast(
            Dict[ComponentId, Connection],
            self._all_dependencies_by_type.get(ComponentType.CONNECTION, {}),
        )

    @property
    def skills(self) -> Dict[ComponentId, Skill]:
        """Get the skills."""
        return cast(
            Dict[ComponentId, Skill],
            self._all_dependencies_by_type.get(ComponentType.SKILL, {}),
        )

    @property
    def contracts(self) -> Dict[ComponentId, Any]:
        """Get the contracts."""
        return cast(
            Dict[ComponentId, Any],
            self._all_dependencies_by_type.get(ComponentType.CONTRACT, {}),
        )

    def add_component(self, component: Component) -> None:
        """Add a component."""
        # add to main index
        self._dependencies[component.component_id] = component
        # add to index by type
        self._all_dependencies_by_type.setdefault(component.component_type, {})[
            component.component_id
        ] = component
        # add to prefix to id index
        self._prefix_to_components.setdefault(
            component.component_id.component_prefix, set()
        ).add(component.component_id)
        # populate inverse dependency
        for dependency in component.configuration.package_dependencies:
            self._inverse_dependency_graph.setdefault(dependency, set()).add(
                component.component_id
            )

    def remove_component(self, component_id: ComponentId):
        """
        Remove a component.

        :return None
        :raises ValueError: if some component depends on this package.
        """
        if component_id not in self.all_dependencies:
            raise ValueError(
                "Component {} of type {} not present.".format(
                    component_id.public_id, component_id.component_type
                )
            )
        dependencies = self._inverse_dependency_graph.get(component_id, set())
        if len(dependencies) != 0:
            raise ValueError(
                "Cannot remove component {} of type {}. Other components depends on it: {}".format(
                    component_id.public_id, component_id.component_type, dependencies
                )
            )

        # remove from the index of all dependencies
        component = self._dependencies.pop(component_id)
        # remove from the index of all dependencies grouped by type
        self._all_dependencies_by_type[component_id.component_type].pop(component_id)
        if len(self._all_dependencies_by_type[component_id.component_type]) == 0:
            self._all_dependencies_by_type.pop(component_id.component_type)
        # remove from prefix to id index
        self._prefix_to_components.get(component_id.component_prefix, set()).discard(
            component_id
        )
        # update inverse dependency graph
        for dependency in component.configuration.package_dependencies:
            self._inverse_dependency_graph[dependency].discard(component_id)

    def check_package_dependencies(
        self, component_configuration: ComponentConfiguration
    ) -> bool:
        """
        Check that we have all the dependencies needed to the package.

        return: True if all the dependencies are covered, False otherwise.
        """
        not_supported_packages = component_configuration.package_dependencies.difference(
            self.all_dependencies
        )
        return len(not_supported_packages) == 0

    @property
    def pypi_dependencies(self) -> Dependencies:
        """Get all the PyPI dependencies."""
        all_pypi_dependencies = {}  # type: Dependencies
        for component in self._dependencies.values():
            # TODO implement merging of two PyPI dependencies.
            all_pypi_dependencies.update(component.configuration.pypi_dependencies)
        return all_pypi_dependencies

    @contextmanager
    def load_dependencies(self):
        """
        Load dependencies of a component, so its modules can be loaded.

        :return: None
        """
        modules = self._get_import_order()
        with _SysModules.load_modules(modules):
            yield

    def _get_import_order(self) -> List[Tuple[str, types.ModuleType]]:
        """
        Get the import order.

        At the moment:
        - protocols and contracts don't have dependencies.
        - a connection can depend on protocols.
        - a skill can depend on protocols and contracts.

        :return: a list of pairs: (import path, module object)
        """
        # get protocols first
        components = filter(
            lambda x: x in self.dependencies_highest_version,
            itertools.chain(
                self.protocols.values(),
                self.contracts.values(),
                self.connections.values(),
                self.skills.values(),
            ),
        )
        module_by_import_path = [
            (self._build_dotted_part(component, relative_import_path), module_obj)
            for component in components
            for (
                relative_import_path,
                module_obj,
            ) in component.importpath_to_module.items()
        ]
        return cast(List[Tuple[str, types.ModuleType]], module_by_import_path)

    @staticmethod
    def _build_dotted_part(component, relative_import_path) -> str:
        """Given a component, build a dotted path for import."""
        if relative_import_path == "":
            return component.prefix_import_path
        else:
            return component.prefix_import_path + "." + relative_import_path


class AEABuilder:
    """
    This class helps to build an AEA.

    It follows the fluent interface. Every method of the builder
    returns the instance of the builder itself.
    """

    def __init__(self, with_default_packages: bool = True):
        """
        Initialize the builder.

        :param with_default_packages: add the default packages.
        """
        self._name = None  # type: Optional[str]
        self._resources = Resources()
        self._private_key_paths = {}  # type: Dict[str, str]
        self._ledger_apis_configs = {}  # type: Dict[str, Dict[str, Union[str, int]]]
        self._default_key = None  # set by the user, or instantiate a default one.
        self._default_ledger = (
            "fetchai"  # set by the user, or instantiate a default one.
        )
        self._default_connection = PublicId("fetchai", "stub", "0.1.0")

        self._package_dependency_manager = _DependenciesManager()

        if with_default_packages:
            self.add_default_packages()

    def add_default_packages(self):
        """Add default packages."""
        # add default protocol
        self.add_protocol(Path(AEA_DIR, "protocols", "default"))
        # add stub connection
        self.add_connection(Path(AEA_DIR, "connections", "stub"))
        # add error skill
        self.add_skill(Path(AEA_DIR, "skills", "error"))

    def _check_can_remove(self, component_id: ComponentId):
        """
        Check if a component can be removed.

        :param component_id: the component id.
        :return: None
        :raises ValueError: if the component is already present.
        """
        if component_id not in self._package_dependency_manager.all_dependencies:
            raise ValueError(
                "Component {} of type {} not present.".format(
                    component_id.public_id, component_id.component_type
                )
            )

    def _check_can_add(self, configuration: ComponentConfiguration) -> None:
        """
        Check if the component can be added, given its configuration.

        :param configuration: the configuration of the component.
        :return: None
        :raises ValueError: if the component is not present.
        """
        self._check_configuration_not_already_added(configuration)
        self._check_package_dependencies(configuration)

    def set_name(self, name: str) -> "AEABuilder":
        """
        Set the name of the agent.

        :param name: the name of the agent.
        """
        self._name = name
        return self

    def set_default_connection(self, public_id: PublicId):
        """
        Set the default connection.

        :param public_id: the public id of the default connection package.
        :return: None
        """
        self._default_connection = public_id

    def add_private_key(
        self, identifier: str, private_key_path: PathLike
    ) -> "AEABuilder":
        """
        Add a private key path.

        :param identifier: the identifier for that private key path.
        :param private_key_path: path to the private key file.
        """
        self._private_key_paths[identifier] = str(private_key_path)
        return self

    def remove_private_key(self, identifier: str) -> "AEABuilder":
        """
        Remove a private key path by identifier, if present.

        :param identifier: the identifier of the private key.

        """
        self._private_key_paths.pop(identifier, None)
        return self

    @property
    def private_key_paths(self) -> Dict[str, str]:
        """Get the private key paths."""
        return self._private_key_paths

    def add_ledger_api_config(self, identifier: str, config: Dict):
        """Add a configuration for a ledger API to be supported by the agent."""
        self._ledger_apis_configs[identifier] = config

    def remove_ledger_api_config(self, identifier: str):
        """Remove a ledger API configuration."""
        self._ledger_apis_configs.pop(identifier, None)

    @property
    def ledger_apis_config(self) -> Dict[str, Dict[str, Union[str, int]]]:
        """Get the ledger api configurations."""
        return self._ledger_apis_configs

    def set_default_ledger_api_config(self, default: str):
        """Set a default ledger API to use."""
        self._default_ledger = default

    def add_component(
        self, component_type: ComponentType, directory: PathLike
    ) -> "AEABuilder":
        """
        Add a component, given its type and the directory.

        :param component_type: the component type.
        :param directory: the directory path.
        :raises ValueError: if a component is already registered with the same component id.
        """
        directory = Path(directory)
        configuration = ComponentConfiguration.load(component_type, directory)
        self._check_can_add(configuration)

        with self._package_dependency_manager.load_dependencies():
            component = Component.load_from_directory(component_type, directory)

        # update dependency graph
        self._package_dependency_manager.add_component(component)
        # register new package in resources
        self._add_component_to_resources(component)

        return self

    def _add_component_to_resources(self, component: Component):
        """Add component to the resources."""
        if component.component_type == ComponentType.CONNECTION:
            # Do nothing - we don't add connections to resources.
            return
        self._resources.add_component(component)

    def _remove_component_from_resources(self, component_id: ComponentId):
        """Remove a component from the resources."""
        if component_id.component_type == ComponentType.CONNECTION:
            return

        if component_id.component_type == ComponentType.PROTOCOL:
            self._resources.remove_protocol(component_id.public_id)
        elif component_id.component_type == ComponentType.SKILL:
            self._resources.remove_skill(component_id.public_id)

    def remove_component(self, component_id: ComponentId) -> "AEABuilder":
        """Remove a component."""
        self._check_can_remove(component_id)
        self._remove(component_id)
        return self

    def _remove(self, component_id: ComponentId):
        self._package_dependency_manager.remove_component(component_id)
        self._remove_component_from_resources(component_id)

    def add_protocol(self, directory: PathLike) -> "AEABuilder":
        """Add a protocol to the agent."""
        self.add_component(ComponentType.PROTOCOL, directory)
        return self

    def remove_protocol(self, public_id: PublicId) -> "AEABuilder":
        """Remove protocol"""
        self.remove_component(ComponentId(ComponentType.PROTOCOL, public_id))
        return self

    def add_connection(self, directory: PathLike) -> "AEABuilder":
        """Add a protocol to the agent."""
        self.add_component(ComponentType.CONNECTION, directory)
        return self

    def remove_connection(self, public_id: PublicId) -> "AEABuilder":
        """Remove a connection"""
        self.remove_component(ComponentId(ComponentType.CONNECTION, public_id))
        return self

    def add_skill(self, directory: PathLike) -> "AEABuilder":
        """Add a skill to the agent."""
        self.add_component(ComponentType.SKILL, directory)
        return self

    def remove_skill(self, public_id: PublicId) -> "AEABuilder":
        """Remove protocol"""
        self.remove_component(ComponentId(ComponentType.SKILL, public_id))
        return self

    def add_contract(self, directory: PathLike) -> "AEABuilder":
        """Add a contract to the agent."""
        self.add_component(ComponentType.CONTRACT, directory)
        return self

    def remove_contract(self, public_id: PublicId) -> "AEABuilder":
        """Remove protocol"""
        self.remove_component(ComponentId(ComponentType.CONTRACT, public_id))
        return self

    def _build_identity_from_wallet(self, wallet: Wallet) -> Identity:
        """Get the identity associated to a wallet."""
        assert self._name is not None, "You must set the name of the agent."
        if len(wallet.addresses) > 1:
            identity = Identity(
                self._name,
                addresses=wallet.addresses,
                default_address_key=self._default_ledger,
            )
        else:  # pragma: no cover
            identity = Identity(
                self._name, address=wallet.addresses[self._default_ledger],
            )
        return identity

    def _process_connection_ids(
        self, connection_ids: Optional[Collection[PublicId]] = None
    ):
        """Process connection ids."""
        if connection_ids is not None:
            # check that all the connections are in the configuration file.
            connection_ids_set = set(connection_ids)
            all_supported_connection_ids = {
                cid.public_id
                for cid in self._package_dependency_manager.connections.keys()
            }
            non_supported_connections = connection_ids_set.difference(
                all_supported_connection_ids
            )
            if len(non_supported_connections) > 0:
                raise ValueError(
                    "Connection ids {} not declared in the configuration file.".format(
                        sorted(map(str, non_supported_connections))
                    )
                )
            connections = [
                connection
                for id_, connection in self._package_dependency_manager.connections.items()
                if id_.public_id in connection_ids_set
            ]
        else:
            connections = list(self._package_dependency_manager.connections.values())

        return connections

    def build(self, connection_ids: Optional[Collection[PublicId]] = None) -> AEA:
        """
        Build the AEA.

        :param connection_ids: select only these connections.
        :return: the AEA object.
        """
        wallet = Wallet(self.private_key_paths)
        identity = self._build_identity_from_wallet(wallet)
        connections = self._process_connection_ids(connection_ids)
        for connection in connections:
            connection.address = identity.address
            connection.load()
        aea = AEA(
            identity,
            connections,
            wallet,
            LedgerApis(self.ledger_apis_config, self._default_ledger),
            self._resources,
            loop=None,
            timeout=0.0,
            is_debug=False,
            is_programmatic=True,
            max_reactions=20,
        )
        self._set_agent_context_to_all_skills(aea.context)
        return aea

    def _set_agent_context_to_all_skills(self, context: AgentContext):
        """Set a skill context to all skills"""
        for skill in self._resources.get_all_skills():
            logger_name = "aea.{}.skills.{}.{}".format(
                context.agent_name, skill.configuration.author, skill.configuration.name
            )
            skill.skill_context.set_agent_context(context)
            skill.skill_context._logger = logging.getLogger(logger_name)

    def _check_configuration_not_already_added(self, configuration):
        if (
            configuration.component_id
            in self._package_dependency_manager.all_dependencies
        ):
            raise ValueError(
                "Component {} of type {} already added.".format(
                    configuration.public_id, configuration.component_type
                )
            )

    def _check_package_dependencies(self, configuration):
        self._package_dependency_manager.check_package_dependencies(configuration)

    @staticmethod
    def _find_component_directory_from_component_id(
        aea_project_directory: Path, component_id: ComponentId
    ):
        """Find a component directory from component id."""
        # search in vendor first
        vendor_package_path = (
            aea_project_directory
            / "vendor"
            / component_id.public_id.author
            / component_id.component_type.to_plural()
            / component_id.public_id.name
        )
        if vendor_package_path.exists() and vendor_package_path.is_dir():
            return vendor_package_path

        # search in custom packages.
        custom_package_path = (
            aea_project_directory
            / component_id.component_type.to_plural()
            / component_id.public_id.name
        )
        if custom_package_path.exists() and custom_package_path.is_dir():
            return custom_package_path

        raise ValueError("Package {} not found.".format(component_id))

    @staticmethod
    def _try_to_load_agent_configuration_file(aea_project_path: Path):
        """Try to load the agent configuration file.."""
        try:
            configuration_file_path = Path(aea_project_path, DEFAULT_AEA_CONFIG_FILE)
            with configuration_file_path.open(mode="r", encoding="utf-8") as fp:
                loader = ConfigLoader.from_configuration_type(ConfigurationType.AGENT)
                agent_configuration = loader.load(fp)
                logging.config.dictConfig(agent_configuration.logging_config)
        except FileNotFoundError:
            raise Exception(
                "Agent configuration file '{}' not found in the current directory.".format(
                    DEFAULT_AEA_CONFIG_FILE
                )
            )
        except jsonschema.exceptions.ValidationError:
            raise Exception(
                "Agent configuration file '{}' is invalid. Please check the documentation.".format(
                    DEFAULT_AEA_CONFIG_FILE
                )
            )

    @classmethod
    def from_aea_project(cls, aea_project_path: PathLike):
        """
        Construct the builder from an AEA project

        - load agent configuration file
        - set name and default configurations
        - load private keys
        - load ledger API configurations
        - set default ledger
        - load every component

        :param aea_project_path: path to the AEA project.
        :return: an AEA agent.
        """
        aea_project_path = Path(aea_project_path)
        cls._try_to_load_agent_configuration_file(aea_project_path)
        _verify_or_create_private_keys(aea_project_path)
        builder = AEABuilder(with_default_packages=False)

        # TODO isolate environment
        # load_env_file(str(aea_config_path / ".env"))

        # load agent configuration file
        configuration_file = aea_project_path / DEFAULT_AEA_CONFIG_FILE

        loader = ConfigLoader.from_configuration_type(ConfigurationType.AGENT)
        agent_configuration = loader.load(configuration_file.open())

        # set name and other configurations
        builder.set_name(agent_configuration.name)
        builder.set_default_ledger_api_config(agent_configuration.default_ledger)
        builder.set_default_connection(
            PublicId.from_str(agent_configuration.default_connection)
        )

        # load private keys
        for (
            ledger_identifier,
            private_key_path,
        ) in agent_configuration.private_key_paths_dict.items():
            builder.add_private_key(ledger_identifier, private_key_path)

        # load ledger API configurations
        for (
            ledger_identifier,
            ledger_api_conf,
        ) in agent_configuration.ledger_apis_dict.items():
            builder.add_ledger_api_config(ledger_identifier, ledger_api_conf)

        component_ids = itertools.chain(
            [
                ComponentId(ComponentType.PROTOCOL, p_id)
                for p_id in agent_configuration.protocols
            ],
            [
                ComponentId(ComponentType.CONTRACT, p_id)
                for p_id in agent_configuration.contracts
            ],
            [
                ComponentId(ComponentType.CONNECTION, p_id)
                for p_id in agent_configuration.connections
            ],
            [
                ComponentId(ComponentType.SKILL, p_id)
                for p_id in agent_configuration.skills
            ],
        )
        for component_id in component_ids:
            component_path = cls._find_component_directory_from_component_id(
                aea_project_path, component_id
            )
            builder.add_component(component_id.component_type, component_path)

        return builder


def _verify_or_create_private_keys(aea_project_path: Path) -> None:
    """Verify or create private keys."""
    path_to_configuration = aea_project_path / DEFAULT_AEA_CONFIG_FILE
    agent_loader = ConfigLoader("aea-config_schema.json", AgentConfig)
    fp_read = path_to_configuration.open(mode="r", encoding="utf-8")
    agent_configuration = agent_loader.load(fp_read)

    for identifier, _value in agent_configuration.private_key_paths.read_all():
        if identifier not in SUPPORTED_CRYPTOS:
            ValueError("Unsupported identifier in private key paths.")

    fetchai_private_key_path = agent_configuration.private_key_paths.read(FETCHAI)
    if fetchai_private_key_path is None:
        _create_fetchai_private_key(
            private_key_file=str(aea_project_path / FETCHAI_PRIVATE_KEY_FILE)
        )
        agent_configuration.private_key_paths.update(FETCHAI, FETCHAI_PRIVATE_KEY_FILE)
    else:
        try:
            _try_validate_fet_private_key_path(
                str(aea_project_path / fetchai_private_key_path), exit_on_error=False
            )
        except FileNotFoundError:  # pragma: no cover
            logger.error(
                "File {} for private key {} not found.".format(
                    repr(fetchai_private_key_path), FETCHAI,
                )
            )
            raise

    ethereum_private_key_path = agent_configuration.private_key_paths.read(ETHEREUM)
    if ethereum_private_key_path is None:
        _create_ethereum_private_key(
            private_key_file=str(aea_project_path / ETHEREUM_PRIVATE_KEY_FILE)
        )
        agent_configuration.private_key_paths.update(
            ETHEREUM, ETHEREUM_PRIVATE_KEY_FILE
        )
    else:
        try:
            _try_validate_ethereum_private_key_path(
                str(aea_project_path / ethereum_private_key_path), exit_on_error=False
            )
        except FileNotFoundError:  # pragma: no cover
            logger.error(
                "File {} for private key {} not found.".format(
                    repr(ethereum_private_key_path), ETHEREUM,
                )
            )
            raise

    fp_write = path_to_configuration.open(mode="w", encoding="utf-8")
    agent_loader.dump(agent_configuration, fp_write)
