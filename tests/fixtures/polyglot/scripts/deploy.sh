#!/usr/bin/env bash
source ./lib/common.sh
. "$HOME/.profile"
# deploy_ghost() { :; }

deploy() {
  echo "deploying $#"
}

function rollback {
  echo "rollback"
}
