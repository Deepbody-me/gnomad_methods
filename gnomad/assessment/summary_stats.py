import logging
from typing import Dict, Optional, Set, Union

import hail as hl

from gnomad.utils.filtering import filter_low_conf_regions
from gnomad.utils.vep import (
    add_most_severe_consequence_to_consequence,
    filter_vep_to_canonical_transcripts,
    get_most_severe_consequence_for_summary,
    process_consequences,
)


logging.basicConfig(format="%(levelname)s (%(name)s %(lineno)s): %(message)s")
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def freq_bin_expr(
    freq_expr: hl.expr.ArrayExpression, index: int = 0
) -> hl.expr.StringExpression:
    """
	Returns case statement adding frequency string annotations based on input AC or AF.

	:param freq_expr: Array of structs containing frequency information.
	:param index: Which index of freq_expr to use for annotation. Default is 0. 
		Assumes freq_expr was calculated with `annotate_freq`.
		Frequency index 0 from `annotate_freq` is frequency for all
		pops calculated on adj genotypes only.
	:return: StringExpression containing bin name based on input AC or AF.
	"""
    return (
        hl.case(missing_false=True)
        .when(freq_expr[index].AC == 0, "Not found")
        .when(freq_expr[index].AC == 1, "Singleton")
        .when(freq_expr[index].AC == 2, "Doubleton")
        .when(freq_expr[index].AC <= 5, "AC 3 - 5")
        .when(freq_expr[index].AF < 1e-4, "AC 6 - 0.01%")
        .when(freq_expr[index].AF < 1e-3, "0.01% - 0.1%")
        .when(freq_expr[index].AF < 1e-2, "0.1% - 1%")
        .when(freq_expr[index].AF < 1e-1, "1% - 10%")
        .when(freq_expr[index].AF > 0.95, ">95%")
        .default("10% - 95%")
    )


def get_summary_counts_dict(
    allele_expr: hl.expr.ArrayExpression,
    lof_expr: hl.expr.StringExpression,
    no_lof_flags_expr: hl.expr.BooleanExpression,
    prefix_str: str = "",
) -> Dict[str, hl.expr.Int64Expression]:
    """
	Returns dictionary containing containing counts of multiple variant categories.

	Categories are:
		- Number of variants
		- Number of indels
		- Number of SNVs
		- Number of LoF variants
		- Number of LoF variants that pass LOFTEE
		- Number of LoF variants that pass LOFTEE without any flgs
		- Number of LoF variants annotated as "other splice" (OS) by LOFTEE
		- Number of LoF variants that fail LOFTEE

	..warning:: 
		Assumes `allele_expr` contains only two variants (multi-allelics have been split).

	:param allele_expr: ArrayExpression containing alleles.
	:param lof_expr: StringExpression containing LOFTEE annotation.
	:param no_lof_flags_expr: BooleanExpression indicating whether LoF variant has any flags.
	:param prefix_str: Desired prefix string for category names. Default is empty str.
	:return: Dict of categories and counts per category.
	"""
    logger.warning("This function expects that multi-allelic variants have been split!")
    return {
        f"{prefix_str}num_variants": hl.agg.count(),
        f"{prefix_str}indels": hl.agg.count_where(
            hl.is_indel(allele_expr[0], allele_expr[1])
        ),
        f"{prefix_str}snps": hl.agg.count_where(
            hl.is_snp(allele_expr[0], allele_expr[1])
        ),
        f"{prefix_str}LOF": hl.agg.count_where(hl.is_defined(lof_expr)),
        f"{prefix_str}pass_loftee": hl.agg.count_where(lof_expr == "HC"),
        f"{prefix_str}pass_loftee_no_flag": hl.agg.count_where(
            (lof_expr == "HC") & (no_lof_flags_expr)
        ),
        f"{prefix_str}loftee_os": hl.agg.count_where(lof_expr == "OS"),
        f"{prefix_str}fail_loftee": hl.agg.count_where(lof_expr == "LC"),
    }


def get_summary_counts(
    ht: hl.Table,
    freq_field: str = "freq",
    filter_field: str = "filters",
    filter_decoy: bool = False,
) -> hl.Table:
    """
	Generates a struct with summary counts across variant categories.

	Summary counts:
		- Number of variants
		- Number of indels
		- Number of SNVs
		- Number of LoF variants
		- Number of LoF variants that pass LOFTEE (including with LoF flags)
		- Number of LoF variants that pass LOFTEE without LoF flags
		- Number of OS (other splice) variants annotated by LOFTEE
		- Number of LoF variants that fail LOFTEE filters

	Also annotates Table's globals with total variant counts.

	Before calculating summary counts, function:
		- Filters out low confidence regions
		- Filters to canonical transcripts
		- Uses the most severe consequence 

	Assumes that:
		- Input HT is annotated with VEP.
		- Multiallelic variants have been split and/or input HT contains bi-allelic variants only.

	:param ht: Input Table.
	:param freq_field: Name of field in HT containing frequency annotation (array of structs). Default is "freq".
	:param filter_field: Name of field in HT containing variant filter information. Default is "filters".
	:param filter_decoy: Whether to filter decoy regions. Default is False.
	:return: Table grouped by frequency bin and aggregated across summary count categories. 
	"""
    logger.info("Filtering to PASS variants in high confidence regions...")
    ht = ht.filter((hl.len(ht[filter_field]) == 0))
    ht = filter_low_conf_regions(ht, filter_decoy=filter_decoy)

    logger.info(
        "Filtering to canonical transcripts and getting VEP summary annotations..."
    )
    ht = filter_vep_to_canonical_transcripts(ht)
    ht = get_most_severe_consequence_for_summary(ht)

    logger.info("Annotating with frequency bin information...")
    ht = ht.annotate(freq_bin=freq_bin_expr(ht[freq_field]))

    logger.info("Annotating HT globals with total counts per variant category...")
    summary_counts = ht.aggregate(
        hl.struct(
            **get_summary_counts_dict(
                ht.alleles, ht.lof, ht.no_lof_flags, prefix_str="total_"
            )
        )
    )
    ht = ht.annotate_globals(summary_counts=summary_counts)
    return ht.group_by("freq_bin").aggregate(
        **get_summary_counts_dict(ht.alleles, ht.lof, ht.no_lof_flags)
    )


def get_an_adj_criteria(
    mt: hl.MatrixTable,
    samples_by_sex: Optional[Dict[str, int]] = None,
    meta_root: str = "meta",
    sex_field: str = "sex_imputation.sex_karyotype",
    xy_str: str = "XY",
    xx_str: str = "XX",
    freq_field: str = "freq",
    freq_index: int = 0,
    an_proportion_cutoff: float = 0.8,
) -> hl.expr.BooleanExpression:
    """
    Generates criteria to filter samples based on allele number (AN).

    Uses allele number as proxy for call rate.

    :param mt: Input MatrixTable.
    :param samples_by_sex: Optional Dictionary containing number of samples (value) for each sample sex (key).
    :param meta_root: Name of field in MatrixTable containing sample metadata information. Default is 'meta'.
    :param sex_field: Name of field in MatrixTable containing sample sex assignment. Defualt is 'sex_imputation.sex_karyotype'.
    :param xy_str: String marking whether a sample has XY sex. Default is 'XY'.
    :param xx_str: String marking whether a sample has XX sex. Default is 'XX'.
    :param freq_field: Name of field in MT that contains frequency information. Default is 'freq'.
    :param freq_index: Which index of frequency struct to use. Default is 0.
    :param an_proportion_cutoff: Desired allele number proportion cutoff. Default is 0.8.
    """
    if samples_by_sex is None:
        samples_by_sex = mt.aggregate_cols(hl.agg.counter(mt[meta_root][sex_field]))
    return (
        hl.case()
        .when(
            mt.locus.in_autosome_or_par(),
            mt[freq_field][freq_index].AN
            >= an_proportion_cutoff * 2 * sum(samples_by_sex.values()),
        )
        .when(
            mt.locus.in_x_nonpar(),
            mt[freq_field][freq_index].AN
            >= an_proportion_cutoff
            * (samples_by_sex[xy_str] + samples_by_sex[xx_str] * 2),
        )
        .when(
            mt.locus.in_y_nonpar(),
            mt[freq_field][freq_index].AN
            >= an_proportion_cutoff * samples_by_sex[xy_str],
        )
        .or_missing()
    )


def annotate_tx_expression_data(
    t: Union[hl.MatrixTable, hl.Table],
    tx_ht: hl.Table,
    csq_expr: hl.expr.StructExpression,
    gene_field: str = "ensg",
    csq_field: str = "csq",
    tx_field: str = "tx_annotation",
):
    key = t.key if isinstance(t, hl.Table) else t.row_key
    return hl.find(
        lambda csq: (csq[gene_field] == csq_expr.gene_id)
        & (csq[csq_field] == csq_expr.most_severe_consequence),
        tx_ht[key][tx_field],
    )


def default_generate_gene_lof_matrix(
    mt: hl.MatrixTable,
    tx_ht: Optional[hl.Table],
    filter_field: str = "filters",
    freq_field: str = "freq",
    freq_index: int = 0,
    additional_csq_set: Set[str] = {"missense_variant", "synonymous_variant"},
    all_transcripts: bool = False,
    filter_an: bool = False,
    filter_to_rare: bool = False,
    pre_loftee: bool = False,
    lof_csq_set: Set[str] = {
        "splice_acceptor_variant",
        "splice_donor_variant",
        "stop_gained",
        "frameshift_variant",
    },
    remove_ultra_common: bool = False,
) -> hl.MatrixTable:
    """

    :param mt: Input MatrixTable.
    :param tx_ht: Optional Table containing expression levels per transcript.
    :param filter_field: Name of field in MT that contains variant filters. Default is 'filters'.
    :param freq_field: Name of field in MT that contains frequency information. Default is 'freq'.
    :param freq_index: Which index of frequency struct to use. Default is 0.
    :param additional_csq_set: Set of additional consequences to keep. Default is {'missense_variant', 'synonymous_variant'}.
    :param all_transcripts: Whether to use all transcripts instead of just the transcript with most severe consequence. Default is False.
    :param filter_an: Whether to filter using allele number as proxy for call rate. Default is False.
    :param samples_by_sex: Optional Dictionary containing number of samples (value) for each sample sex (key).
    :param meta_root: Name of field in MatrixTable containing sample metadata information. Default is 'meta'.
    :param sex_field: Name of field in MatrixTable containing sample sex assignment. Defualt is 'sex_imputation.sex_karyotype'.
    :param xy_str: String marking whether a sample has XY sex. Default is 'XY'.
    :param xx_str: String marking whether a sample has XX sex. Default is 'XX'.
    :param an_proportion_cutoff: Desired allele number proportion cutoff. Default is 0.8.
    :param filter_to_rare: Whether to filter to rare (AF < 5%) variants. Default is False.
    :param pre_loftee: Whether LoF consequences have been annotated with LOFTEE. Default is False.
    :param lof_csq_set: Set of LoF consequence strings. Default is {"splice_acceptor_variant", "splice_donor_variant", "stop_gained", "frameshift_variant"}.
    :param remove_ultra_common: Whether to remove ultra common (AF > 95%) variants. Default is False.
    """
    filt_criteria = hl.len(mt[filter_field]) == 0
    if filter_an:
        filt_criteria &= get_an_adj_criteria(mt)
    if remove_ultra_common:
        filt_criteria &= mt[freq_field][freq_index].AF < 0.95
    if filter_to_rare:
        filt_criteria &= mt[freq_field][freq_index].AF < 0.05
    mt = mt.filter_rows(filt_criteria)

    if all_transcripts:
        explode_field = "transcript_consequences"
    else:
        mt = process_consequences(mt)
        explode_field = "worst_csq_by_gene"

    if pre_loftee:
        lof_cats = hl.literal(lof_csq_set)
        criteria = lambda x: lof_cats.contains(
            add_most_severe_consequence_to_consequence(x).most_severe_consequence
        )
    else:
        criteria = lambda x: (x.lof == "HC") & hl.is_missing(x.lof_flags)

    if additional_csq_set:
        additional_cats = hl.literal(additional_csq_set)
        criteria &= lambda x: additional_cats.contains(
            add_most_severe_consequence_to_consequence(x).most_severe_consequence
        )

    csqs = mt.vep[explode_field].filter(criteria)
    mt = mt.select_rows(mt[freq_field], csqs=csqs)
    mt = mt.explode_rows(mt.csqs)
    annotation_expr = {
        "gene_id": mt.csqs.gene_id,
        "gene": mt.csqs.gene_symbol,
        "indel": hl.is_indel(mt.alleles[0], mt.alleles[1]),
        "most_severe_consequence": mt.csqs.most_severe_consequence,
    }

    if tx_ht:
        tx_annotation = annotate_tx_expression_data(mt, tx_ht, mt.csqs).mean_expression
        annotation_expr["expressed"] = (
            hl.case()
            .when(tx_annotation >= 0.9, "high")
            .when(tx_annotation > 0.1, "medium")
            .when(hl.is_defined(tx_annotation), "low")
            .default("missing")
        )
    else:
        annotation_expr["transcript_id"] = mt.csqs.transcript_id
        annotation_expr["canonical"] = hl.is_defined(mt.csqs.canonical)
    mt = mt.annotate_rows(**annotation_expr)

    return (
        mt.group_rows_by(*list(annotation_expr.keys()))
        .aggregate_rows(
            n_sites=hl.agg.count(),
            n_sites_array=hl.agg.array_sum(mt.freq.map(lambda x: hl.int(x.AC > 0))),
            classic_caf=hl.agg.sum(mt[freq_field][freq_index].AF),
            max_af=hl.agg.max(mt[freq_field][freq_index].AF),
            classic_caf_array=hl.agg.array_sum(mt[freq_field].map(lambda x: x.AF)),
        )
        .aggregate_entries(
            num_homs=hl.agg.count_where(mt.GT.is_hom_var()),
            num_hets=hl.agg.count_where(mt.GT.is_het()),
            defined_sites=hl.agg.count_where(hl.is_defined(mt.GT)),
        )
        .result()
    )
