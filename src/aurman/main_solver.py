import logging
import sys
from typing import Sequence, Set

from pycman.config import PacmanConfig

from aurman.classes import System, Package
from aurman.coloring import aurman_error, Colors
from aurman.parse_args import parse_pacman_args, PacmanOperations
from aurman.utilities import version_comparison, strip_versioning_from_name

# you may want to switch to logging.DEBUG
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(module)s - %(funcName)s - %(levelname)s - %(message)s')


def sanitize_user_input(user_input: Sequence[str], system: 'System') -> Set[str]:
    """
    Finds the names of the packages for the user_input
    Needed since user may also specify the version of a package,
    hence package1>1.0 may yield package1 since package1 has version 2.0

    :param user_input:      The user input in a sequence
    :param system:          The system to check the providing of
    :return:                A set containing the packages names
    """
    sanitized_names = set()
    for name in user_input:
        providers_for_name = system.provided_by(name)
        if not providers_for_name:
            aurman_error("No providers for {} found.".format(Colors.BOLD(Colors.LIGHT_MAGENTA(name))))
            sys.exit(1)
        elif len(providers_for_name) == 1:
            sanitized_names.add(providers_for_name[0].name)
        # more than one provider
        else:
            dep_name = strip_versioning_from_name(name)
            providers_names = [package.name for package in providers_for_name]
            if dep_name in providers_names:
                sanitized_names.add(dep_name)
            else:
                aurman_error("Multiple providers found for {}\n"
                             "None of the providers has the name "
                             "of the dep without versioning.\n"
                             "The providers are: {}".format(Colors.BOLD(Colors.LIGHT_MAGENTA(name)), ", ".join(
                    [system.repo_of_package(provider_for_name) for provider_for_name in providers_names])))
                sys.exit(1)

    return sanitized_names


def process(args):
    import aurman.aur_utilities

    # parse parameters of user
    pacman_args = parse_pacman_args(args)

    if pacman_args.operation is not PacmanOperations.SYNC or pacman_args.invalid_args:
        sys.exit(1)

    # -S or --sync
    packages_of_user_names = list(set(pacman_args.targets))  # targets of the aurman command without duplicates
    sysupgrade = pacman_args.sysupgrade  # if -u or --sysupgrade

    # nothing to do for us
    if not sysupgrade and not packages_of_user_names:
        sys.exit(1)

    needed = pacman_args.needed  # if --needed
    only_unfulfilled_deps = not pacman_args.deep_search  # if not --deep_search

    not_remove = pacman_args.holdpkg  # list containing the specified packages for --holdpkg
    # if --holdpkg_conf append holdpkg from pacman.conf
    if pacman_args.holdpkg_conf:
        not_remove.extend(PacmanConfig(conf="/etc/pacman.conf").options['HoldPkg'])
    # remove duplicates
    not_remove = list(set(not_remove))

    aur = pacman_args.aur  # do only aur things
    repo = pacman_args.repo  # do only repo things
    if repo and aur:
        aurman_error("--repo and --aur is not what you want")
        sys.exit(1)

    if pacman_args.domain:
        aurman.aur_utilities.aur_domain = pacman_args.domain[0]

    # analyzing installed packages
    installed_system = System(System.get_installed_packages())

    if installed_system.not_repo_not_aur_packages_list:
        logging.debug("the following packages are neither in known repos nor in the aur")
        for package in installed_system.not_repo_not_aur_packages_list:
            logging.debug("{}".format(Colors.BOLD(Colors.LIGHT_MAGENTA(package))))

    # fetching upstream repo packages...
    if not aur:
        upstream_system = System(System.get_repo_packages())
    else:
        upstream_system = System(())

    # fetching needed aur packages
    if not repo:
        upstream_system.append_packages_by_name(packages_of_user_names)
        # fetch info for all installed aur packages, too
        names_of_installed_aur_packages = [package.name for package in installed_system.aur_packages_list]
        names_of_installed_aur_packages.extend([package.name for package in installed_system.devel_packages_list])
        upstream_system.append_packages_by_name(names_of_installed_aur_packages)

    # sanitize user input
    sanitized_names = sanitize_user_input(packages_of_user_names, upstream_system)
    sanitized_not_to_be_removed = sanitize_user_input(not_remove, installed_system)

    # names to not be removed must be also known on the upstream system,
    # otherwise aurman solving cannot handle this case.
    for name in sanitized_not_to_be_removed:
        if name not in upstream_system.all_packages_dict:
            aurman_error("Packages you want to be not removed must be aur or repo packages.\n"
                         "   {} is not known.".format(Colors.BOLD(Colors.LIGHT_MAGENTA(name))))
            sys.exit(1)

    # for dep solving not to be removed has to be treated as wanted to install
    sanitized_names |= sanitized_not_to_be_removed

    # fetching ignored packages
    ignored_packages_names = Package.get_ignored_packages_names(pacman_args.ignore, pacman_args.ignoregroup,
                                                                upstream_system)
    # explicitly typed in names will not be ignored
    ignored_packages_names -= sanitized_names
    for ignored_packages_name in ignored_packages_names:
        if ignored_packages_name in upstream_system.all_packages_dict:
            if ignored_packages_name in installed_system.all_packages_dict:
                logging.debug("{} {} package {}".format(Colors.BOLD(Colors.LIGHT_MAGENTA("Ignoring")),
                                                        Colors.BOLD(Colors.LIGHT_CYAN("installed")),
                                                        Colors.BOLD(Colors.LIGHT_MAGENTA(ignored_packages_name))))

                upstream_system.all_packages_dict[ignored_packages_name] = installed_system.all_packages_dict[
                    ignored_packages_name]
            else:
                logging.debug("{} {} package {}".format(Colors.BOLD(Colors.LIGHT_MAGENTA("Ignoring")),
                                                        Colors.BOLD(Colors.LIGHT_BLUE("upstream ")),
                                                        Colors.BOLD(Colors.LIGHT_MAGENTA(ignored_packages_name))))

                del upstream_system.all_packages_dict[ignored_packages_name]

    # recreating upstream system
    if ignored_packages_names:
        upstream_system = System(list(upstream_system.all_packages_dict.values()))

    # checking which packages need to be installed
    if not needed:
        concrete_packages_to_install = [upstream_system.all_packages_dict[name] for name in sanitized_names]
    else:
        possible_packages = [upstream_system.all_packages_dict[name] for name in sanitized_names]
        concrete_packages_to_install = []
        for package in possible_packages:
            if package.name in installed_system.all_packages_dict:
                installed_package = installed_system.all_packages_dict[package.name]
                if version_comparison(installed_package.version, "<", package.version):
                    concrete_packages_to_install.append(package)
            else:
                concrete_packages_to_install.append(package)

    # in case of sysupgrade and not --repo fetch all installed aur packages, of which newer versions are available
    if sysupgrade and not repo:
        installed_aur_packages = [package for package in installed_system.aur_packages_list]
        installed_aur_packages.extend([package for package in installed_system.devel_packages_list])
        for package in installed_aur_packages:
            # must not be that we have not received the upstream information
            assert package.name in upstream_system.all_packages_dict
            upstream_package = upstream_system.all_packages_dict[package.name]
            if version_comparison(upstream_package.version, ">", package.version):
                if upstream_package not in concrete_packages_to_install:
                    concrete_packages_to_install.append(upstream_package)

    # calc solutions
    if only_unfulfilled_deps:
        solutions = Package.dep_solving(concrete_packages_to_install, installed_system, upstream_system)
    else:
        solutions = Package.dep_solving(concrete_packages_to_install, System(()), upstream_system)

    # fetch valid solutions
    valid_solutions = [sol_tuple[1] for sol_tuple in
                       installed_system.validate_solutions(solutions, concrete_packages_to_install)]
    if not valid_solutions:
        aurman_error("we could not find a solution")
        aurman_error("if you think that there should be one, rerun aurman with the --deep_search flag")
        sys.exit(1)

    print(valid_solutions)


def main():
    try:
        process(sys.argv[1:])
    except:
        sys.exit(1)


if __name__ == '__main__':
    main()
