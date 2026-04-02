def normalize_info($info):
  {
    name: ($info.name // ""),
    description: ($info.description // ""),
    tags: (if ($info.tags | type) == "array" then $info.tags else [] end),
    avatar_url: ($info.avatar_url // "")
  };

def normalize_deps($deps):
  if ($deps | type) != "object" then
    {}
  else
    $deps
    | to_entries
    | map(select(.key != null and .key != ""))
    | sort_by(.key)
    | from_entries
  end;

def version_key:
  . as $version
  | ($version | capture("^(?<core>[^-]+)(?:-(?<pre>.*))?$")) as $parts
  | [
      ([ $parts.core | scan("[0-9]+|[A-Za-z]+") ]
       | map(if test("^[0-9]+$") then [0, tonumber] else [1, .] end)),
      (if ($parts.pre // "") == "" then 1 else 0 end),
      ([ ($parts.pre // "") | scan("[0-9]+|[A-Za-z]+") ]
       | map(if test("^[0-9]+$") then [0, tonumber] else [1, .] end)),
      $version
    ];

def package_meta($name):
  {
    stargazer_count:
      ($existing_index[0].packages[$name].stargazer_count
       // $existing_levi[0].packages[$name].stargazer_count
       // 0),
    updated_at:
      ($existing_index[0].packages[$name].updated_at
       // $existing_levi[0].packages[$name].updated_at
       // "")
  };

def root_template($existing):
  {
    format_version: ($existing.format_version // 3),
    format_uuid: ($existing.format_uuid // "289f771f-2c9a-4d73-9f3f-8492495a924d")
  };

def ordered_variants($variants; $for_levilauncher):
  $variants
  | to_entries
  | sort_by(if .key == "" then 0 else 1 end, .key)
  | reduce .[] as $variant ({};
      .[$variant.key] = {
        versions:
          (if $for_levilauncher then
             $variant.value.versions
             | to_entries
             | sort_by(.key | version_key)
             | reduce .[] as $version ({};
                 .[$version.key] = {
                   dependencies: normalize_deps($version.value.dependencies // {})
                 })
           else
             $variant.value.versions
             | keys
             | sort_by(version_key)
           end)
      });

def aggregate_packages:
  reduce $tooths[0][] as $tooth
    ({};
      .[$tooth.tooth] = (
        .[$tooth.tooth]
        // (
          package_meta($tooth.tooth)
          + {
              info: normalize_info($tooth.info // {}),
              variants: {}
            }
        )
        | .info = normalize_info($tooth.info // {})
        | reduce ($tooth.variants // [])[] as $variant (.;
            .variants[$variant.label // ""] = (
              .variants[$variant.label // ""] // { versions: {} }
              | .versions[$tooth.version] = (
                  .versions[$tooth.version] // { dependencies: {} }
                  | .dependencies += normalize_deps($variant.dependencies // {})
                )
            ))
      )
    );

aggregate_packages as $packages
| {
    index:
      (root_template(($existing_index[0] // $existing_levi[0] // {}))
       + {
           packages:
             ($packages
              | to_entries
              | sort_by(.key)
              | reduce .[] as $package ({};
                  .[$package.key] = (
                    $package.value
                    | .variants = ordered_variants(.variants; false)
                  )))
         }),
    levilauncher:
      (root_template(($existing_levi[0] // $existing_index[0] // {}))
       + {
           packages:
             ($packages
              | to_entries
              | sort_by(.key)
              | reduce .[] as $package ({};
                  .[$package.key] = (
                    $package.value
                    | .variants = ordered_variants(.variants; true)
                  )))
         })
  }
