#!/usr/bin/env bash
# E1 - merge semantics and merge base
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]:-$0}")"
source ./common.sh

C0="jomeio4x95rlw5e2nuaawgp1qepuvwv"
OUT="$EVID/E1"
mkdir -p "$OUT"

run_trial() {
  local trial="$1" main_price="$2" main_stock="$3" feat_price="$4" feat_stock="$5"
  for variant in a b c; do
    local tag="${trial}_${variant}"
    {
      echo "=== Trial $trial / Variant $variant ==="
      # 1. clean reset
      echo "--- reset main and feature to C0 ($C0) ---"
      reset_branch main "$C0"; echo
      reset_branch feature "$C0"; echo

      # 2. diverge
      echo "--- PUT main: price=$main_price stock=$main_stock ---"
      put_widget main "$main_price" "$main_stock" "trial-$trial-main"; echo
      echo "--- PUT feature: price=$feat_price stock=$feat_stock ---"
      put_widget feature "$feat_price" "$feat_stock" "trial-$trial-feature"; echo

      local main_tip feature_tip before
      main_tip="$(branch_tip main)"
      feature_tip="$(branch_tip feature)"
      echo "main_tip=$main_tip"
      echo "feature_tip=$feature_tip"

      case "$variant" in
        a) before="$C0" ;;
        b) before="$main_tip" ;;
        c) before="" ;;
      esac
      echo "before_commit variant ($variant) = '$before'"

      # 3. apply
      if [ -n "$before" ]; then
        body="{\"before_commit\":\"$before\",\"after_commit\":\"$feature_tip\",\"commit_info\":{\"author\":\"spike\",\"message\":\"apply-$tag\"},\"type\":\"instance\"}"
      else
        body="{\"after_commit\":\"$feature_tip\",\"commit_info\":{\"author\":\"spike\",\"message\":\"apply-$tag\"},\"type\":\"instance\"}"
      fi
      echo "--- POST /api/apply/$ORG/$DB/local/branch/main ---"
      echo "REQUEST BODY: $body"
      curl_json POST "/api/apply/$ORG/$DB/local/branch/main" "$body"
      echo

      # 4. read back main
      echo "--- read back main/Product/Widget ---"
      read_widget main
      echo
      echo "=== end $tag ==="
    } | tee "$OUT/trial_${tag}.txt"
  done
}

# Trial A: non-overlapping. main: stock 5->7. feature: price 9.99->14.99.
run_trial A 9.99 7 14.99 5

# Trial B: same-field. main: price 9.99->12.99. feature: price 9.99->14.99.
run_trial B 12.99 5 14.99 5

echo "DONE" | tee "$OUT/_done.txt"
