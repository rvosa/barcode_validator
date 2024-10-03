import io
import tarfile
from typing import List, Optional
from barcode_validator.taxonomy import BlastRunner
from barcode_validator.alignment import SequenceHandler
from barcode_validator.result import DNAAnalysisResult
from Bio.SeqRecord import SeqRecord
from Bio.Phylo.BaseTree import Tree
from nbitk.config import Config
from nbitk.logger import get_formatted_logger
from nbitk.Taxon import Taxon
from nbitk.Phylo.NCBITaxdmp import Parser as NCBIParser
from nbitk.Phylo.BOLDXLSXIO import Parser as BOLDParser


class BarcodeValidator:
    def __init__(self, config: Config):
        """
        Initialize the BarcodeValidator object.
        """
        self.ncbi_taxonomy = config.get('ncbi_taxonomy')
        self.ncbi_tree: Optional[Tree] = None
        self.bold_xlsx_file = config.get('bold_sheet_file')
        self.bold_tree: Optional[Tree] = None
        class_name = self.__class__.__name__
        self.logger = get_formatted_logger(class_name, config)

    def initialize(self) -> None:
        """
        Initialize the taxonomy trees. This is separate from __init__ to allow for lazy loading.
        :return: None
        """
        self.logger.info("Initializing taxonomy trees...")
        self.ncbi_tree = NCBIParser(tarfile.open(self.ncbi_taxonomy, "r:gz")).parse()
        with open(self.bold_xlsx_file, 'rb') as file:
            excel_data = io.BytesIO(file.read())
            self.bold_tree = BOLDParser(excel_data).parse()
        self.logger.info("Initialization complete.")

    def validate_fasta(self, fasta_file_path: str, config: Config) -> List[DNAAnalysisResult]:
        """
        Validate a FASTA file of DNA sequences.
        :param fasta_file_path: A path to a FASTA file
        :param config: A Config object
        :return: A list of DNAAnalysisResult objects
        """
        results = []
        sh = SequenceHandler(config)
        for process_id, record, json_config in sh.parse_fasta(fasta_file_path):
            scoped_config = config.local_clone(json_config)
            result = self.validate_record(process_id, record, scoped_config)
            results.append(result)
        return results

    def validate_record(self, process_id: str, record: SeqRecord, config: Config) -> DNAAnalysisResult:
        """
        Validate a single DNA sequence record.
        :param process_id: A process ID
        :param record: A Bio.SeqRecord object
        :param config: A Config object
        :return: A DNAAnalysisResult object
        """
        result = DNAAnalysisResult(process_id)
        self.validate_sequence_quality(config, record, result)
        self.validate_taxonomy(config, record, result)

        # Return the result object
        return result

    def validate_taxonomy(self, config: Config, record: SeqRecord, result: DNAAnalysisResult) -> None:
        """
        Validate the taxonomy of a DNA sequence record.
        :param config: A Config object
        :param record: A Bio.SeqRecord object
        :param result: A DNAAnalysisResult object
        """

        # Lookup expected taxon in BOLD tree
        sp = self.get_node_by_processid(result.process_id)
        if sp is None:
            self.logger.warning(f"Process ID {result.process_id} not found in BOLD tree.")
            result.error = f"{result.process_id} not in BOLD"
        else:

            # Traverse BOLD tree to find the expected taxon at the specified rank
            result.species = sp
            self.logger.info(f"Species: {result.species}")
            for node in self.bold_tree.root.get_path(result.species):
                if node.taxonomic_rank == config.get('level'):
                    result.exp_taxon = node
                    break

            # Run local BLAST to find observed taxon at the specified rank
            constraint = self.build_constraint(sp, config.get('constrain'))
            br = BlastRunner(config)
            br.ncbi_tree = self.ncbi_tree
            obs_taxon = br.run_localblast(record, constraint, config.get('level'))

            # Handle BLAST failure
            if obs_taxon is None:
                self.logger.warning(f"Local BLAST failed for {result.process_id}")
                result.error = f"Local BLAST failed for sequence '{record.seq}'"
            else:
                result.obs_taxon = obs_taxon

    def get_node_by_processid(self, process_id):
        """
        Get a node from the BOLD tree by its 'processid' attribute.
        :param process_id: A process ID
        :return: A Taxon object
        """
        self.logger.info(f'Looking up tip by process ID: {process_id}')
        for node in self.bold_tree.find_clades():
            if process_id in node.guids:
                return node
        return None

    def build_constraint(self, bold_tip: Taxon, rank: str) -> str:
        """
        Given a tip from the BOLD tree, looks up its path to the root, fetching the interior node at the specified
        taxonomic rank. Then, traverses the NCBI tree to find the identically-named node at the same rank and returns
        its taxon ID.
        :param bold_tip: A Taxon object from the BOLD tree
        :param rank: A taxonomic rank to constrain the search to
        :return: An NCBI taxon ID
        """

        # Find the node at the specified taxonomic rank in the BOLD lineage that subtends the tip
        self.logger.info(f"Going to traverse BOLD taxonomy for the {rank} to which {bold_tip} belongs")
        bold_anc = next(node for node in self.bold_tree.root.get_path(bold_tip) if node.taxonomic_rank == rank)
        self.logger.info(f"BOLD {bold_tip.taxonomic_rank} {bold_tip.name} is member of {rank} {bold_anc.name}")

        # Find the corresponding node at the same rank in the NCBI tree
        ncbi_anc = next(node for node in self.ncbi_tree.get_nonterminals()
                        if node.name == bold_anc.name and node.taxonomic_rank == rank)

        # Return or die
        if ncbi_anc:
            self.logger.info(f"Corresponding ncbi node is taxon:{ncbi_anc.guids['taxon']}")
            return ncbi_anc.guids['taxon']
        else:
            raise ValueError(f"Could not find NCBI node for BOLD node '{bold_anc}'")

    def validate_sequence_quality(self, config: Config, record: SeqRecord, result: DNAAnalysisResult) -> None:
        """
        Validate the quality of a DNA sequence record.
        :param config: A Config object
        :param record: A Bio.SeqRecord object
        :param result: A DNAAnalysisResult object
        :return: None
        """

        # Instantiate result object with process ID and calculate full sequence stats
        result.full_length = len(record.seq)
        sh = SequenceHandler(config)
        result.full_ambiguities = sh.num_ambiguous(record)

        # Compute marker quality metrics
        aligned_sequence = sh.align_to_hmm(record)
        if aligned_sequence is None:
            self.logger.warning(f"Alignment failed for {result.process_id}")
            result.error = f"Alignment failed for sequence '{record.seq}'"
        else:
            amino_acid_sequence = sh.translate_sequence(aligned_sequence, config.get('translation_table'))
            result.stop_codons = sh.get_stop_codons(amino_acid_sequence)
            result.seq_length = sh.marker_seqlength(aligned_sequence)
            result.ambiguities = sh.num_ambiguous(aligned_sequence)
