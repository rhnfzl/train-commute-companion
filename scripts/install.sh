#!/usr/bin/env bash
# Install one private Compose project per traveller from a matching release.
set +x
set -euo pipefail
umask 077

die() {
    printf '%s\n' "$*" >&2
    exit 1
}

version=${1:-}
target=${2:-./train-commute-companion}
[[ $# -le 2 && $version =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] ||
    die "Usage: bash install.sh RELEASE_VERSION [INSTALL_DIRECTORY]"
[[ $target = /* ]] || target="./$target"

check_target() {
    [[ ! -L $target ]] || die "Choose an install directory that is not a symlink."
    if [[ -e $target ]]; then
        [[ -d $target ]] || die "The install path is not a directory."
        local first
        first=$(find "$target/." -mindepth 1 -maxdepth 1 -print -quit)
        [[ ! $first ]] || die "The install directory is not empty."
    fi
}

check_target
docker compose version --short >/dev/null || die "Install Docker with Compose first."
docker info >/dev/null 2>&1 || die "Start Docker and check access to the Docker engine."
command -v curl >/dev/null || die "Install curl first."
exec 3<>/dev/tty || die "Run the installer in an interactive terminal."
tty_settings=$(stty -g <&3)
trap 'stty "$tty_settings" <&3' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

read_secret() {
    stty -echo <&3
    printf '%s' "$1" >&3
    IFS= read -r "$2" <&3
    stty "$tty_settings" <&3
    printf '\n' >&3
}

printf 'How many travellers? [1] ' >&3
IFS= read -r count <&3
count=${count:-1}
if [[ ! $count =~ ^[1-9][0-9]{0,4}$ ]] || (( count > 65535 - 8080 + 1 )); then
    die "Enter a positive traveller count that fits the local port range."
fi
ns_key="" bot_token=""
for ((i=0; i<count; i++)); do
    fallback="traveller-$((i + 1))"
    printf 'Folder name for traveller %d [%s]: ' "$((i + 1))" "$fallback" >&3
    IFS= read -r name <&3
    name=${name:-$fallback}
    [[ $name =~ ^[a-z0-9][a-z0-9_-]*$ ]] ||
        die "Use lower-case letters, digits, underscores, or hyphens for folder names."
    read_secret "Personal NS-App key for $name: " ns_key
    read_secret "Private BotFather token for $name: " bot_token
    [[ $ns_key =~ ^[a-zA-Z0-9._+/=-]+$ ]] || die "Enter a valid NS-App primary key."
    [[ $bot_token =~ ^([1-9][0-9]*):[a-zA-Z0-9_-]+$ ]] || die "Enter a valid BotFather token."
    bot_id=${BASH_REMATCH[1]}
    for ((j=0; j<i; j++)); do
        [[ ${names[j]} != "$name" ]] || die "Use a different folder name for each traveller."
        [[ ${bot_ids[j]} != "$bot_id" ]] || die "Use a separate bot for each traveller."
        [[ ${ns_keys[j]} != "$ns_key" ]] || die "Use a personal NS-App key for each traveller."
    done
    names[i]=$name
    ns_keys[i]=$ns_key
    bot_tokens[i]=$bot_token
    bot_ids[i]=$bot_id
done
trap - EXIT INT TERM
exec 3>&-

source_url="https://raw.githubusercontent.com/rhnfzl/train-commute-companion/v$version/examples"
compose=$(curl --proto '=https' -fsSL "$source_url/compose.setup.yaml") ||
    die "Release v$version does not supply the setup compose file."
[[ $compose == *'@TCC_INSTALL_IMAGE@'* && $compose == *'@TCC_INSTALL_PORT@'* ]] ||
    die "The setup compose file does not match this installer."
image="ghcr.io/rhnfzl/train-commute-companion:$version"
digest=$(docker buildx imagetools inspect "$image" --format '{{.Manifest.Digest}}') ||
    die "Could not resolve the image from release v$version. Check Docker Buildx and registry access."
[[ $digest =~ ^sha256:[0-9a-f]{64}$ ]] || die "The registry did not return a usable image digest."
compose=${compose//@TCC_INSTALL_IMAGE@/$image@$digest}
project_id=$(od -An -N16 -tx1 /dev/urandom | tr -d '[:space:]')
[[ $project_id =~ ^[0-9a-f]{32}$ ]] || die "Could not create a private Compose project name."
check_target
mkdir -p -- "$target"
cd -- "$target"
root=$(pwd -P)
for ((i=0; i<count; i++)); do
    dir="$root/${names[i]}"
    project="tcc-$project_id-$i"
    port=$((8080 + i))
    mkdir -- "$dir"
    (
        set -C
        printf 'name: %s\n%s\n' "$project" "${compose//@TCC_INSTALL_PORT@/$port}" > "$dir/compose.yaml"
        printf 'COMPOSE_PROJECT_NAME=%s\n' "$project" > "$dir/.env"
        printf 'NS_API_KEY=%s\nTELEGRAM_BOT_TOKEN=%s\n' \
            "${ns_keys[i]}" "${bot_tokens[i]}" >> "$dir/.env"
    )
done
failed=0
for ((i=0; i<count; i++)); do
    dir="$root/${names[i]}"
    project="tcc-$project_id-$i"
    command=(docker compose --project-name "$project" --file compose.yaml --env-file .env)
    if (cd -- "$dir" && "${command[@]}" config --quiet && "${command[@]}" up -d tcc); then
        printf 'Private claim link: cd %q && docker compose --project-name %q --file compose.yaml --env-file .env logs tcc\n' \
            "$dir" "$project"
    else
        printf 'Retry after fixing the Compose error: cd %q && docker compose --project-name %q --file compose.yaml --env-file .env up -d tcc\n' \
            "$dir" "$project" >&2
        failed=1
    fi
done
printf '%s\n' 'Keep the setup logs private. Open your own claim link and run /setup in Telegram.'
exit "$failed"
